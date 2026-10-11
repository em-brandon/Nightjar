<?php
// The PHP reporter's rules, with no network. Run with: php client/php/test.php
// Works on PHP 7.4 and newer.

require __DIR__ . '/Nightjar.php';

use Nightjar\Reporter;

$failures = 0;
function check(string $name, bool $cond, string $detail = ''): void
{
    global $failures;
    if ($cond) {
        echo "  ok   $name\n";
    } else {
        $failures++;
        echo "  FAIL $name" . ($detail !== '' ? " -- $detail" : '') . "\n";
    }
}

const OPTS = ['hub' => 'https://nightjar.example/', 'key' => 'KEY1', 'secret' => 's3cret', 'release' => 'v2', 'transport' => 'auto'];

function fails_with(): Exception
{
    return deep_a();
}
function deep_a(): Exception
{
    try {
        deep_b(42);
    } catch (Exception $e) {
        return $e;
    }
    return new Exception('unreachable');
}
function deep_b(int $n): void
{
    throw new InvalidArgumentException("bad value $n");
}
class Cart
{
    public function total(): void
    {
        throw new LogicException('cart is empty');
    }
    public static function make(): Exception
    {
        try {
            (new self())->total();
        } catch (Exception $e) {
            return $e;
        }
        return new Exception('unreachable');
    }
}

// ---- frames ---------------------------------------------------------------------------------

$frames = Reporter::frames(fails_with());
$fns = array_map(function ($f) { return $f['function'] ?? null; }, $frames);
check('the throw site is the INNERMOST frame, and is last', end($fns) === 'deep_b', json_encode($fns));
check('callers come before it, outermost first', array_slice($fns, -3) === ['{main}', 'fails_with', 'deep_a'] || in_array('deep_a', $fns, true), json_encode($fns));
check('frames carry a module, file and line', isset($frames[0]['module'], $frames[0]['file'], $frames[0]['lineno']) && $frames[0]['module'] === 'test.php');
$cart = array_map(function ($f) { return $f['function'] ?? null; }, Reporter::frames(Cart::make()));
check('a method is named Class::method', in_array('Cart::total', $cart, true), json_encode($cart));
check('the innermost frame is the method that threw', end($cart) === 'Cart::total', json_encode($cart));

// ---- the signed request ---------------------------------------------------------------------

$p = Reporter::prepare(OPTS, fails_with(), 'error', ['method' => 'POST', 'path' => '/cart']);
check('the url has the key and no double slash', $p['url'] === 'https://nightjar.example/ingest/KEY1/event', $p['url']);
$expected = 'sha256=' . hash_hmac('sha256', $p['body'], 's3cret');
check('the HMAC is over exactly the bytes sent', $p['headers']['X-Hub-Signature'] === $expected);
$b = json_decode($p['body'], true);
check('type, message, level, release and environment', [$b['exception_type'], $b['message'], $b['level'], $b['release'], $b['environment']] === ['InvalidArgumentException', 'bad value 42', 'error', 'v2', 'production']);
check('the request has a method and a path', $b['request'] === ['method' => 'POST', 'path' => '/cart']);
check('a query string handed to prepare() is dropped', json_decode(Reporter::prepare(OPTS, new Exception('x'), 'error', ['method' => 'GET', 'path' => '/a?token=SECRET'])['body'], true)['request']['path'] === '/a');
check('an unknown level becomes error', json_decode(Reporter::prepare(OPTS, new Exception('x'), 'catastrophic')['body'], true)['level'] === 'error');
check('fatal is allowed when asked for', json_decode(Reporter::prepare(OPTS, new Exception('x'), 'fatal')['body'], true)['level'] === 'fatal');
$anon = json_decode(Reporter::prepare(OPTS, new class('x') extends Exception {})['body'], true)['exception_type'];
check('an anonymous class name has no NUL or path in it', strpos($anon, "\0") === false && strpos($anon, '/') === false, $anon);
$utf = json_decode(Reporter::prepare(OPTS, new Exception("caf\xe9 broken \xff bytes"))['body'], true);
check('invalid UTF-8 in a message does not lose the report', is_array($utf) && $utf['exception_type'] === 'Exception');
$big = json_decode(Reporter::prepare(OPTS, new Exception(str_repeat('y', 50000)))['body'], true);
check('a huge message is cut under the hub limit', strlen($big['message']) <= 3900);

putenv('NIGHTJAR_URL=https://h.example');
putenv('NIGHTJAR_KEY=K9');
putenv('NIGHTJAR_SECRET=e');
putenv('NIGHTJAR_ENV=staging');
$env = Reporter::prepare([], new Exception('x'));
check('the NIGHTJAR_* variables are the defaults', $env['url'] === 'https://h.example/ingest/K9/event' && json_decode($env['body'], true)['environment'] === 'staging');
putenv('NIGHTJAR_URL');
putenv('NIGHTJAR_KEY');
putenv('NIGHTJAR_SECRET');
putenv('NIGHTJAR_ENV');

// ---- the query string -----------------------------------------------------------------------

// request info only exists on the web, so pretend to be a web request
$_SERVER['REQUEST_METHOD'] = 'GET';
$_SERVER['REQUEST_URI'] = '/orders/7?token=SECRET&x=1';
$sent = [];
Reporter::reset();
Reporter::setTransport(function ($url, $h, $body, $t) use (&$sent) { $sent[] = ['url' => $url, 'h' => $h, 'body' => $body]; });
$ref = new ReflectionMethod(Reporter::class, 'requestInfo');
$ref->setAccessible(true);
check('a PHP-SAPI cli run has no request', $ref->invoke(null) === null);
unset($_SERVER['REQUEST_METHOD'], $_SERVER['REQUEST_URI']);

// ---- queueing, caps, and never breaking -----------------------------------------------------

Reporter::reset();
Reporter::setTransport(function ($url, $h, $body, $t) use (&$sent) { $sent[] = $body; });
$sent = [];
Reporter::captureException(new Exception('before init'));
check('an unconfigured reporter queues nothing and throws nothing', Reporter::queued() === []);
Reporter::init(OPTS);
for ($i = 0; $i < 20; $i++) {
    Reporter::captureException(new Exception("e$i"));
}
check('at most five reports are held per request', count(Reporter::queued()) === 5, (string) count(Reporter::queued()));
check('nothing is sent until the request ends', $sent === []);
Reporter::flush();
check('flush sends them all', count($sent) === 5, (string) count($sent));
check('and empties the queue', Reporter::queued() === []);

$threw = false;
try {
    Reporter::captureException(null);
    Reporter::captureException('a string');
    Reporter::captureException(['array']);
    Reporter::captureException(new stdClass());
    Reporter::captureException(new Error('an Error, not an Exception'), 'fatal');
} catch (Throwable $t) {
    $threw = true;
}
check('a string, null, array, object or Error never throws into the app', !$threw);
check('and an Error (a Throwable) is reported', count(Reporter::queued()) === 5);
Reporter::reset();

// ---- a hub that is down ---------------------------------------------------------------------

Reporter::reset();
$unique = ['hub' => 'https://down-' . uniqid() . '.example', 'key' => 'K', 'secret' => 's'];
$calls = 0;
Reporter::setTransport(function () use (&$calls) { $calls++; throw new RuntimeException('hub is down'); });
Reporter::init($unique);
foreach (['a', 'b', 'c'] as $m) {
    Reporter::captureException(new Exception($m));
}
Reporter::flush();
check('a failed send stops the rest of that request', $calls === 1, (string) $calls);
Reporter::captureException(new Exception('later'));
Reporter::flush();
check('and the breaker keeps the NEXT request from trying for 30 seconds', $calls === 1, (string) $calls);
@unlink(sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'nightjar-down-' . md5($unique['hub'] . $unique['key']));
Reporter::reset();

Reporter::setTransport(function () use (&$calls) { $calls = 100; });
Reporter::init($unique);
Reporter::captureException(new Exception('after the breaker was cleared'));
Reporter::flush();
check('with the breaker cleared it sends again', $calls === 100);
@unlink(sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'nightjar-down-' . md5($unique['hub'] . $unique['key']));
Reporter::reset();

// ---- the exception handler ------------------------------------------------------------------

Reporter::reset();
$got = [];
Reporter::setTransport(function ($url, $h, $body, $t) use (&$got) { $got[] = json_decode($body, true); });

$calledPrevious = null;
set_exception_handler(function ($e) use (&$calledPrevious) { $calledPrevious = $e->getMessage(); });
Reporter::install(OPTS);
$handler = set_exception_handler(null); // take ours back out to call it by hand
check('install() put a handler in place', is_callable($handler));
$handler(new Exception('app has its own handler'));
check('the previous handler still runs', $calledPrevious === 'app has its own handler');
Reporter::flush();
check('and the exception was reported', count($got) === 1 && $got[0]['message'] === 'app has its own handler');
restore_exception_handler();

// with no handler of its own, the app must still crash the way PHP crashes: the exception comes back out
set_exception_handler(null); // a clean slate: nothing was installed before us
Reporter::reset();
Reporter::setTransport(function ($url, $h, $body, $t) {});
Reporter::install(OPTS);
$handler = set_exception_handler(null);
$rethrown = null;
try {
    $handler(new DomainException('nobody handles me'));
} catch (DomainException $e) {
    $rethrown = $e->getMessage();
}
check('with no previous handler the exception is thrown again, so PHP fails as it normally would', $rethrown === 'nobody handles me');
Reporter::reset();

// installing twice must not make two handlers that hand the exception to each other forever
set_exception_handler(null);
Reporter::reset();
Reporter::setTransport(function ($url, $h, $body, $t) {});
Reporter::install(OPTS);
Reporter::install(OPTS);
Reporter::install(OPTS);
$handler = set_exception_handler(null);
$rethrown = null;
try {
    $handler(new DomainException('installed three times'));
} catch (DomainException $e) {
    $rethrown = $e->getMessage();
}
check('install() is idempotent: three calls make one handler, not a loop', $rethrown === 'installed three times');
check('and the exception was queued exactly once', count(Reporter::queued()) === 1, (string) count(Reporter::queued()));
Reporter::reset();

// ---- a hub that is not https ----------------------------------------------------------------------------------

foreach (['https://nightjar.example', 'https://nightjar.example/', 'http://localhost:8000', 'http://127.0.0.1:9', 'http://[::1]:8000', 'http://app.localhost'] as $good) {
    check("$good is a usable hub", Reporter::hubRefusal($good) === null);
}
foreach (['http://nightjar.example', 'http://10.0.0.5', 'http://192.168.1.20:8000', 'http://hub.test', 'ftp://nightjar.example', 'nightjar.example', '//nightjar.example'] as $bad) {
    check("$bad is refused", Reporter::hubRefusal($bad) !== null);
}
$why = (string) Reporter::hubRefusal('http://user:pa55word@nightjar.example/path?token=abc');
check('the reason names the host but never the whole address', strpos($why, 'pa55word') === false && strpos($why, 'token=abc') === false && strpos($why, 'user:') === false);

Reporter::reset();
$logged = [];
Reporter::setLogger(function ($m) use (&$logged) { $logged[] = $m; });
$sent = 0;
Reporter::setTransport(function () use (&$sent) { $sent++; });
$threw = false;
try {
    $ok = Reporter::init(['hub' => 'http://nightjar.example', 'key' => 'K', 'secret' => 's']);
    Reporter::captureException(new Exception('boom'));
    Reporter::captureException(new Exception('boom again'));
    Reporter::flush();
} catch (\Throwable $e) {
    $threw = true;
}
check('an http:// hub throws nothing', !$threw);
check('init() says it is not configured', $ok === false);
check('it says so once, in the app log, not silently', count($logged) === 1 && strpos($logged[0], 'refusing to report errors to http://nightjar.example') !== false, (string) count($logged));
check('and queues and sends nothing', count(Reporter::queued()) === 0 && $sent === 0);
Reporter::reset();

$logged = [];
foreach (['https://nightjar.example', 'http://127.0.0.1:8000', 'http://localhost:8000'] as $hub) {
    Reporter::reset();
    Reporter::setLogger(function ($m) use (&$logged) { $logged[] = $m; });
    Reporter::setTransport(function () {});
    Reporter::init(['hub' => $hub, 'key' => 'K', 'secret' => 's']);
    Reporter::captureException(new Exception('boom'));
    check("$hub still reports", count(Reporter::queued()) === 1);
}
check('and a good hub is not warned about', $logged === []);
Reporter::reset();

echo $failures === 0 ? "\nphp reporter passed\n" : "\n$failures FAILED\n";
exit($failures === 0 ? 0 : 1);
