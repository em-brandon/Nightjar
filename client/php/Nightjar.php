<?php
/**
 * Nightjar reporter for PHP (plain PHP, Laravel, WordPress). One file, no dependencies: require it.
 *
 *     require __DIR__ . '/Nightjar.php';
 *     \Nightjar\Reporter::install();     // reads NIGHTJAR_URL, NIGHTJAR_KEY and NIGHTJAR_SECRET
 *
 * install() reports uncaught exceptions and fatal errors. \Nightjar\Reporter::captureException($e) is for
 * exceptions you catch yourself (Laravel's report() hook and WordPress snippets are in docs/REPORTERS.md).
 *
 * This holds the site's ingest SECRET, so it belongs on a server, never in a page. See docs/KEYS.md.
 *
 * The rule it shares with client/reporter.py: it must never break the app it reports on. Every path is
 * wrapped and swallowed, a send has a short timeout and no retry, at most five reports are held per request,
 * and under PHP-FPM the report is sent after the response has already gone to the visitor. A circuit
 * breaker stops a request from paying for a hub that is down: after a failure nothing is sent for 30 seconds.
 *
 * Works on PHP 7.4 and newer. Uses the curl extension if there is one and plain streams if not.
 */

namespace Nightjar;

final class Reporter
{
    const LEVELS = ['warning', 'error', 'fatal'];
    const MAX_PER_REQUEST = 5;
    const MAX_FRAMES = 100;
    const MAX_FIELD = 200;
    const MAX_MESSAGE = 3900; // the hub allows 4000
    const BREAKER_SECONDS = 30;

    /** @var array|null */
    private static $options = null;
    /** @var array[] prepared requests waiting for the end of the request */
    private static $queue = [];
    private static $hooked = false;
    private static $uncaught = false;
    private static $installed = false;
    private static $warned = false;
    /** @var callable|null for tests: where the one-line warning about an unusable hub goes (default: error_log) */
    private static $logger = null;
    /** @var callable|null for tests: function (string $url, array $headers, string $body, int $timeout): void */
    private static $transport = null;
    /** @var int|null when the hub last failed in this process: the breaker's fallback if the temp directory is unusable */
    private static $trippedAt = null;

    /**
     * Whether this hub address may be used, and if not, why. https is always fine. Plain http is fine only for a machine
     * talking to itself, which development needs: anywhere else it would send error text, readable, across a network.
     * Returns null when the address is fine, or one line saying what is wrong. It never includes the whole address,
     * which could carry a credential.
     */
    public static function hubRefusal(string $hub): ?string
    {
            $p = parse_url($hub);
        if ($p === false || !isset($p['scheme'], $p['host'])) {
            return 'Nightjar: the hub address is not a valid URL, so no errors will be reported. Use https://your-hub.';
        }
        $scheme = strtolower($p['scheme']);
        $host = strtolower($p['host']);
        if ($scheme === 'https') {
            return null;
        }
        if ($scheme === 'http' && (in_array($host, ['localhost', '127.0.0.1', '[::1]', '::1'], true) || substr($host, -10) === '.localhost')) {
            return null;
        }
        return "Nightjar: refusing to report errors to {$scheme}://{$host}: plain http would send them readable across the network. "
            . 'Use an https:// hub address (http:// is only accepted for localhost). No errors will be reported.';
    }

    /** Configure. Returns false (and does nothing else) if the hub, key or secret is missing, or the hub is not https. */
    public static function init(array $options = []): bool
    {
        try {
            self::$options = self::resolve($options);
            self::warnIfRefused(self::$options);
            return self::configured(self::$options);
        } catch (\Throwable $e) {
            return false;
        }
    }

    /** init() plus the handlers: reports uncaught exceptions and fatal errors, and changes nothing else. */
    public static function install(array $options = []): void
    {
        try {
            self::init($options);
            self::hook();
            // A second call (two plugins, or a worker that boots twice) must not stack a second handler on
            // the first: the newer one would hand the exception to the older, and they would loop.
            if (self::$installed) {
                return;
            }
            self::$installed = true;
            // The handler reports, then lets PHP do exactly what it would have done: call the handler that
            // was there before, or fail with PHP's own "Uncaught ..." fatal error.
            $previous = null;
            $previous = set_exception_handler(function ($e) use (&$previous) {
                self::$uncaught = true;
                self::captureException($e, 'error');
                if ($previous !== null) {
                    call_user_func($previous, $e);
                    return;
                }
                // While a handler runs PHP has no handler set, so throwing here gives PHP's own
                // "Uncaught ..." fatal error. (Calling restore_exception_handler() first would put THIS
                // handler back and loop forever.)
                throw $e;
            });
        } catch (\Throwable $e) {
            // reporting must never break the app
        }
    }

    /** Report an exception you caught yourself. Sent when the request ends. */
    public static function captureException($e, string $level = 'error'): void
    {
        try {
            $o = self::options();
            if (!self::configured($o) || count(self::$queue) >= self::MAX_PER_REQUEST) {
                return;
            }
            if (!($e instanceof \Throwable)) {
                $e = new \RuntimeException(is_string($e) ? $e : 'non-exception thrown');
            }
            self::$queue[] = self::prepare($o, $e, $level, self::requestInfo());
            self::hook();
        } catch (\Throwable $x) {
            // never throw into the caller
        }
    }

    /**
     * The signed request for one exception. Public so a test can check exactly what the hub will see.
     *
     * @return array{url:string, headers:array<string,string>, body:string}
     */
    public static function prepare(array $options, \Throwable $e, string $level = 'error', ?array $request = null): array
    {
        $o = self::resolve($options);
        $type = get_class($e);
        $type = explode("\0", $type)[0]; // an anonymous class's name has a NUL and a file path in it
        $payload = [
            'exception_type' => self::cut($type, self::MAX_FIELD),
            'message' => self::cut(self::scrub((string) $e->getMessage()), self::MAX_MESSAGE),
            'frames' => self::frames($e),
            'level' => in_array($level, self::LEVELS, true) ? $level : 'error',
            'environment' => $o['environment'],
        ];
        if ($o['release'] !== null) {
            $payload['release'] = $o['release'];
        }
        if ($request !== null) {
            if (isset($request['path']) && is_string($request['path'])) {
                $request['path'] = explode('?', $request['path'], 2)[0]; // never a query string
            }
            $payload['request'] = $request;
        }
        return self::sign($o, $payload);
    }

    /** Frames for a Throwable, innermost LAST, which is the order the hub expects. */
    public static function frames(\Throwable $e): array
    {
        $trace = $e->getTrace();
        $name = function (array $t) {
            return (isset($t['class']) ? $t['class'] . '::' : '') . ($t['function'] ?? '');
        };
        // PHP's trace lists, for each call, WHERE it was called from. The place the exception was thrown
        // is not in the trace, so it is added first, named after the function that was running.
        $inner = [self::frame($e->getFile(), $e->getLine(), isset($trace[0]) ? $name($trace[0]) : '{main}')];
        foreach ($trace as $i => $t) {
            if (!isset($t['file'])) {
                continue; // an internal call, such as array_map invoking a closure
            }
            $inner[] = self::frame($t['file'], $t['line'] ?? null, isset($trace[$i + 1]) ? $name($trace[$i + 1]) : '{main}');
        }
        return array_slice(array_reverse($inner), -self::MAX_FRAMES);
    }

    /** Send what is waiting. Runs by itself at the end of the request; call it before a long-running script exits. */
    public static function flush(): void
    {
        try {
            if (!self::$queue) {
                return;
            }
            $jobs = self::$queue;
            self::$queue = [];
            // PHP-FPM: let the visitor go first, so a slow hub costs them nothing
            $fpm = function_exists('fastcgi_finish_request');
            if ($fpm) {
                @fastcgi_finish_request();
            } else {
                // No way to close the connection here (mod_php, the built-in server): at least push the page out
                // before the sends, so the visitor sees it even while the worker is still busy.
                while (PHP_SAPI !== 'cli' && ob_get_level() > 0 && @ob_end_flush()) {
                }
                @flush();
            }
            $o = self::options();
            if (self::breakerOpen($o)) {
                return;
            }
            foreach ($jobs as $job) {
                try {
                    // without FPM the worker is held for the whole send, so it gets one second, not three
                    self::post($job['url'], $job['headers'], $job['body'], $fpm ? $o['timeout'] : min(1, $o['timeout']), $o['transport']);
                    self::breakerClear($o);
                } catch (\Throwable $failed) {
                    self::breakerTrip($o);
                    break; // the hub is not answering; do not wait on the rest
                }
            }
        } catch (\Throwable $e) {
            // nothing to do
        }
    }

    /** Runs when the request ends: report a fatal error that no handler could catch, then send. */
    public static function onShutdown(): void
    {
        try {
            $last = error_get_last();
            $fatal = [E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR, E_USER_ERROR];
            // an uncaught exception also ends as E_ERROR; it has been reported already, with a real trace
            if ($last && in_array($last['type'], $fatal, true) && !self::$uncaught) {
                $o = self::options();
                if (self::configured($o) && count(self::$queue) < self::MAX_PER_REQUEST) {
                    self::$queue[] = self::sign($o, [
                        'exception_type' => 'FatalError',
                        'message' => self::cut(self::scrub((string) $last['message']), self::MAX_MESSAGE),
                        'frames' => [self::frame((string) $last['file'], $last['line'], null)],
                        'level' => 'error',
                        'environment' => $o['environment'],
                        'request' => self::requestInfo(),
                    ]);
                }
            }
        } catch (\Throwable $e) {
            // never throw from a shutdown function
        }
        self::flush();
    }

    /** For tests: replace the network. */
    public static function setTransport(?callable $transport): void
    {
        self::$transport = $transport;
    }

    /** For tests: replace error_log as the place the unusable-hub warning goes. */
    public static function setLogger(?callable $logger): void
    {
        self::$logger = $logger;
    }

    /** For tests: forget everything. */
    public static function reset(): void
    {
        self::$options = null;
        self::$queue = [];
        self::$uncaught = false;
        self::$installed = false;
        self::$transport = null;
        self::$warned = false;
        self::$logger = null;
        self::$trippedAt = null;
    }

    /** For tests: what is waiting to be sent. */
    public static function queued(): array
    {
        return self::$queue;
    }

    // ---- internals --------------------------------------------------------------------------------

    private static function hook(): void
    {
        if (!self::$hooked) {
            self::$hooked = true;
            register_shutdown_function([self::class, 'onShutdown']);
        }
    }

    private static function options(): array
    {
        if (self::$options === null) {
            self::$options = self::resolve([]);
            self::warnIfRefused(self::$options);
        }
        return self::$options;
    }

    /**
     * An unusable hub is never silent: a mistyped address would otherwise look exactly like an app with no errors. It
     * says so once per process, to the app's own log, and then sends nothing. It never throws, because a reporter must
     * not break the app it reports on.
     */
    private static function warnIfRefused(array $o): void
    {
        try {
            if (self::$warned || $o['hub'] === '') {
                return;
            }
            $why = self::hubRefusal($o['hub']);
            if ($why === null) {
                return;
            }
            self::$warned = true;
            if (self::$logger !== null) {
                call_user_func(self::$logger, $why);
            } else {
                error_log($why);
            }
        } catch (\Throwable $e) {
            // even the warning must not break the app
        }
    }

    private static function resolve(array $o): array
    {
        $env = function (string $name) {
            $v = getenv($name);
            return $v === false || $v === '' ? null : $v;
        };
        return [
            'hub' => rtrim((string) ($o['hub'] ?? $env('NIGHTJAR_URL') ?? ''), '/'),
            'key' => (string) ($o['key'] ?? $env('NIGHTJAR_KEY') ?? ''),
            'secret' => (string) ($o['secret'] ?? $env('NIGHTJAR_SECRET') ?? ''),
            'environment' => (string) ($o['environment'] ?? $env('NIGHTJAR_ENV') ?? 'production'),
            'release' => isset($o['release']) ? (string) $o['release'] : $env('NIGHTJAR_RELEASE'),
            'timeout' => (int) ($o['timeout'] ?? 3),
            'transport' => (string) ($o['transport'] ?? 'auto'), // auto | curl | stream
            'breaker_dir' => (string) ($o['breaker_dir'] ?? $env('NIGHTJAR_BREAKER_DIR') ?? ''), // default: the system temp directory
        ];
    }

    private static function configured(array $o): bool
    {
        return $o['hub'] !== '' && $o['key'] !== '' && $o['secret'] !== '' && self::hubRefusal($o['hub']) === null;
    }

    private static function sign(array $o, array $payload): array
    {
        $body = json_encode(
            $payload,
            JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE | JSON_PARTIAL_OUTPUT_ON_ERROR
        );
        if ($body === false) {
            $body = '{"exception_type":"Unencodable","message":"the report could not be encoded","level":"error"}';
        }
        // the HMAC is over the timestamp, a dot and exactly the bytes that are sent, which is what the hub verifies;
        // the timestamp is inside the signature so a captured report cannot be replayed later
        $timestamp = (string) time();
        return [
            'url' => $o['hub'] . '/ingest/' . $o['key'] . '/event',
            'headers' => [
                'Content-Type' => 'application/json',
                'X-Hub-Timestamp' => $timestamp,
                'X-Hub-Signature' => 'sha256=' . hash_hmac('sha256', $timestamp . '.' . $body, $o['secret']),
            ],
            'body' => $body,
        ];
    }

    private static function frame(string $file, $line, ?string $function): array
    {
        $file = self::relativePath(str_replace('\\', '/', $file));
        $frame = ['file' => self::cut($file, self::MAX_FIELD), 'module' => self::cut(basename($file), self::MAX_FIELD)];
        if ($line !== null) {
            $frame['lineno'] = (int) $line;
        }
        if ($function !== null && $function !== '') {
            $frame['function'] = self::cut($function, self::MAX_FIELD);
        }
        return $frame;
    }

    /** The method and path only. A query string can carry a token or a person's details. */
    private static function requestInfo(): ?array
    {
        if (PHP_SAPI === 'cli' || !isset($_SERVER['REQUEST_METHOD'])) {
            return null;
        }
        $path = parse_url((string) ($_SERVER['REQUEST_URI'] ?? ''), PHP_URL_PATH);
        return ['method' => (string) $_SERVER['REQUEST_METHOD'], 'path' => is_string($path) ? $path : null];
    }

    /**
     * Take the obvious secrets out of an exception message before it leaves the server. Best effort, not a guarantee:
     * a message can hold anything, so do not put secrets in exception messages. The hub also normalises quoted
     * strings and numbers for fingerprinting, so this does not change how errors group.
     */
    public static function scrub(string $m): string
    {
        $m = preg_replace('#\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}#i', '$1 [redacted]', $m) ?? $m;
        $m = preg_replace('~(?<=://)[^/\s:@]+:[^/\s@]+@~', '[redacted]@', $m) ?? $m;
        $m = preg_replace('~\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization)(["\']?\s*[=:]\s*["\']?)[^\s"\'&,;]+~i', '$1$2[redacted]', $m) ?? $m;
        $m = preg_replace('~[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}~', '[email]', $m) ?? $m;
        return $m;
    }

    /** A path without the server's own directory layout (home directories, account names): relative to the document root or working directory. */
    private static function relativePath(string $file): string
    {
        $bases = [];
        foreach ([$_SERVER['DOCUMENT_ROOT'] ?? '', @getcwd()] as $b) {
            if (is_string($b) && $b !== '') {
                $bases[] = rtrim(str_replace('\\', '/', $b), '/') . '/';
            }
        }
        foreach ($bases as $b) {
            if (strncmp($file, $b, strlen($b)) === 0) {
                return substr($file, strlen($b));
            }
        }
        return $file;
    }

    private static function cut(string $s, int $max): string
    {
        return function_exists('mb_substr') ? mb_substr($s, 0, $max, 'UTF-8') : substr($s, 0, $max);
    }

    private static function post(string $url, array $headers, string $body, int $timeout, string $mode): void
    {
        if (self::$transport !== null) {
            call_user_func(self::$transport, $url, $headers, $body, $timeout);
            return;
        }
        $lines = [];
        foreach ($headers as $k => $v) {
            $lines[] = $k . ': ' . $v;
        }
        if ($mode !== 'stream' && function_exists('curl_init')) {
            $ch = curl_init($url);
            curl_setopt_array($ch, [
                CURLOPT_POST => true,
                CURLOPT_POSTFIELDS => $body,
                CURLOPT_HTTPHEADER => $lines,
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_TIMEOUT => $timeout,
                CURLOPT_CONNECTTIMEOUT => 1,
                CURLOPT_NOSIGNAL => true, // so a short timeout works on every platform
            ]);
            $result = curl_exec($ch);
            $failed = $result === false;
            if (PHP_VERSION_ID < 80000) {
                curl_close($ch); // a no-op since PHP 8.0, and deprecated in 8.5
            }
            if ($failed) {
                throw new \RuntimeException('curl failed');
            }
            return;
        }
        $context = stream_context_create(['http' => [
            'method' => 'POST',
            'header' => implode("\r\n", $lines),
            'content' => $body,
            'timeout' => $timeout,
            'ignore_errors' => true, // an HTTP error status is an answer, not a failure to reach the hub
        ]]);
        if (@file_get_contents($url, false, $context) === false) {
            throw new \RuntimeException('stream failed');
        }
    }

    // A file's age is the breaker: it works across PHP-FPM workers with no extension and no daemon.

    private static function breakerPath(array $o): string
    {
        $dir = $o['breaker_dir'] !== '' ? rtrim($o['breaker_dir'], '/\\') : sys_get_temp_dir();
        return $dir . DIRECTORY_SEPARATOR . 'nightjar-down-' . md5($o['hub'] . $o['key']);
    }

    private static function breakerOpen(array $o): bool
    {
        $path = self::breakerPath($o);
        $age = @filemtime($path);
        if ($age === false) {
            $age = self::$trippedAt; // no file: either the hub is fine, or the directory is not writable
        }
        return $age !== null && $age !== false && (time() - $age) < self::BREAKER_SECONDS;
    }

    private static function breakerTrip(array $o): void
    {
        self::$trippedAt = time(); // kept in memory too, for a long-running worker whose temp directory cannot be written
        @touch(self::breakerPath($o));
    }

    private static function breakerClear(array $o): void
    {
        self::$trippedAt = null;
        $path = self::breakerPath($o);
        if (file_exists($path)) {
            @unlink($path);
        }
    }
}
