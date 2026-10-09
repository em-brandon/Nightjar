package nightjar;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Nightjar reporter for Java (Spring, Jakarta, plain JVM). One file, no dependencies beyond the JDK: copy it in.
 *
 * <pre>
 *   Nightjar.init();                       // reads NIGHTJAR_URL, NIGHTJAR_KEY and NIGHTJAR_SECRET
 *   Nightjar.installUncaughtHandler();     // report a thread that dies
 *   Nightjar.captureException(e);          // an exception you caught yourself
 * </pre>
 *
 * This holds the site's ingest SECRET, so it belongs on a server, never in a page. See docs/KEYS.md.
 *
 * The rule it shares with client/reporter.py: it must never break the app it reports on. Everything is wrapped
 * and swallowed, a send has a short timeout and no retry, reports go through ONE daemon thread behind a queue
 * of at most five (anything past that is dropped, never held), and so a Nightjar outage costs the app nothing.
 *
 * Needs Java 11 or newer (java.net.http). Compile it with --release 11 to prove it.
 *
 * Two choices worth knowing. A wrapped exception (a Spring or servlet wrapper around the real failure) is
 * reported as its ROOT CAUSE, because the wrapper's own stack trace points at the wrapping and nothing else;
 * the wrappers are listed under "extra". And generated class names that change on every JVM start (CGLIB
 * proxies, lambdas) are normalised, as a bundler's file hashes are in the browser, so a restart does not make
 * every error look new.
 */
public final class Nightjar {
    private Nightjar() {}

    public enum Level {
        WARNING("warning"), ERROR("error"), FATAL("fatal");
        final String wire;
        Level(String wire) { this.wire = wire; }
    }

    /** Settings. Anything left null falls back to NIGHTJAR_URL, NIGHTJAR_KEY, NIGHTJAR_SECRET, NIGHTJAR_ENV, NIGHTJAR_RELEASE. */
    public static final class Options {
        public String hub;
        public String key;
        public String secret;
        public String environment;
        public String release;
        /** How long one send may take. Default 3000 ms. */
        public int timeoutMs = 3000;
        /** Most reports sent in a minute; the rest are dropped. Default 60. */
        public int maxPerMinute = 60;
        /** Most reports waiting at once; the rest are dropped. Default 5. */
        public int maxQueued = 5;
        /** Level for a thread that dies. Default ERROR (P2): a pool thread dying is not the service dying. */
        public Level uncaughtLevel = Level.ERROR;

        public Options hub(String v) { this.hub = v; return this; }
        public Options key(String v) { this.key = v; return this; }
        public Options secret(String v) { this.secret = v; return this; }
        public Options environment(String v) { this.environment = v; return this; }
        public Options release(String v) { this.release = v; return this; }
    }

    /** One signed request, ready to send. Public so a test can check exactly what the hub will see. */
    public static final class Prepared {
        public final String url;
        public final Map<String, String> headers;
        public final String body;
        Prepared(String url, Map<String, String> headers, String body) {
            this.url = url;
            this.headers = headers;
            this.body = body;
        }
    }

    /** The network, replaceable in tests. */
    public interface Transport {
        void send(String url, Map<String, String> headers, String body, int timeoutMs) throws Exception;
    }

    private static final int MAX_MESSAGE = 3900; // the hub allows 4000
    private static final int MAX_FRAMES = 100; // the hub allows 200; the innermost matter most
    private static final int MAX_FIELD = 200;
    private static final int MAX_CAUSES = 20;

    private static volatile Options active;
    private static volatile ThreadPoolExecutor pool;
    private static volatile Transport transport = Nightjar::httpSend;
    private static final ArrayDeque<Long> sentAt = new ArrayDeque<>();
    private static final AtomicBoolean handlerInstalled = new AtomicBoolean(false);

    // ---- setup --------------------------------------------------------------------------------

    /** Configure from the environment. Returns false (and does nothing) if the hub, key or secret is missing. */
    public static boolean init() {
        return init(new Options());
    }

    public static boolean init(Options options) {
        try {
            Options o = resolve(options);
            if (!configured(o)) {
                return false;
            }
            ThreadPoolExecutor p = new ThreadPoolExecutor(
                    1, 1, 0L, TimeUnit.MILLISECONDS,
                    new ArrayBlockingQueue<Runnable>(Math.max(1, o.maxQueued)),
                    r -> {
                        Thread t = new Thread(r, "nightjar-reporter");
                        t.setDaemon(true); // never keep the JVM alive, and never delay its exit
                        return t;
                    },
                    new ThreadPoolExecutor.DiscardPolicy()); // full means drop, not wait and not grow
            active = o;
            pool = p;
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    // ---- reporting ----------------------------------------------------------------------------

    public static void captureException(Throwable t) {
        captureException(t, Level.ERROR, null, null);
    }

    public static void captureException(Throwable t, Level level) {
        captureException(t, level, null, null);
    }

    /** Report an exception, with the request it happened in (method and path; any query string is dropped). */
    public static void captureException(Throwable t, Level level, String method, String path) {
        try {
            Options o = active;
            ThreadPoolExecutor p = pool;
            if (o == null || p == null || !allowed(o)) {
                return;
            }
            final Prepared prepared = prepare(o, t, level, method, path);
            final Transport tr = transport;
            final int timeout = o.timeoutMs;
            p.execute(() -> {
                try {
                    tr.send(prepared.url, prepared.headers, prepared.body, timeout);
                } catch (Throwable ignored) {
                    // a failed report is not the app's problem
                }
            });
        } catch (Throwable ignored) {
            // reporting must never break the app
        }
    }

    /** Wait (up to timeoutMs) for reports already on their way, for example before a deliberate shutdown. */
    public static void flush(long timeoutMs) {
        try {
            ThreadPoolExecutor p = pool;
            if (p == null) {
                return;
            }
            p.submit(() -> { }).get(timeoutMs, TimeUnit.MILLISECONDS); // runs after everything already queued
        } catch (Throwable ignored) {
            // gave up waiting; nothing else to do
        }
    }

    /**
     * Report a thread that dies from an uncaught exception, then do what the JVM would have done: call the
     * handler that was there before, or print the stack trace as the JVM does. The report is sent before
     * returning (for at most two seconds), because the JVM may be about to exit and a queue would be lost.
     */
    public static void installUncaughtHandler() {
        try {
            if (!handlerInstalled.compareAndSet(false, true)) {
                return; // a second call would make a handler that hands the exception to itself
            }
            final Thread.UncaughtExceptionHandler previous = Thread.getDefaultUncaughtExceptionHandler();
            Thread.setDefaultUncaughtExceptionHandler((thread, error) -> {
                try {
                    Options o = active;
                    if (o != null) {
                        Prepared p = prepare(o, error, o.uncaughtLevel, null, null);
                        transport.send(p.url, p.headers, p.body, Math.min(o.timeoutMs, 2000));
                    }
                } catch (Throwable ignored) {
                    // the thread is dying anyway; report what we can and carry on
                }
                if (previous != null) {
                    previous.uncaughtException(thread, error);
                } else {
                    System.err.print("Exception in thread \"" + thread.getName() + "\" ");
                    error.printStackTrace();
                }
            });
        } catch (Throwable ignored) {
            // reporting must never break the app
        }
    }

    // ---- the request --------------------------------------------------------------------------

    /** The signed request for one exception. */
    public static Prepared prepare(Options options, Throwable error, Level level, String method, String path) {
        Options o = resolve(options);
        List<Throwable> chain = causeChain(error);
        Throwable root = chain.get(chain.size() - 1);

        StringBuilder b = new StringBuilder(1024);
        b.append("{\"exception_type\":");
        str(b, cut(root.getClass().getName(), MAX_FIELD));
        b.append(",\"message\":");
        str(b, cut(String.valueOf(root.getMessage() == null ? "" : root.getMessage()), MAX_MESSAGE));
        b.append(",\"frames\":");
        frames(b, root);
        b.append(",\"level\":");
        str(b, (level == null ? Level.ERROR : level).wire);
        b.append(",\"environment\":");
        str(b, o.environment);
        if (o.release != null) {
            b.append(",\"release\":");
            str(b, o.release);
        }
        if (method != null || path != null) {
            b.append(",\"request\":{\"method\":");
            str(b, method == null ? "" : cut(method, 16));
            b.append(",\"path\":");
            str(b, path == null ? "" : cut(path.split("\\?", 2)[0], 500)); // never a query string
            b.append('}');
        }
        if (chain.size() > 1) {
            b.append(",\"extra\":{\"wrapped_by\":[");
            for (int i = 0; i < chain.size() - 1 && i < 5; i++) {
                if (i > 0) {
                    b.append(',');
                }
                Throwable w = chain.get(i);
                str(b, cut(w.getClass().getName() + ": " + w.getMessage(), MAX_FIELD));
            }
            b.append("]}");
        }
        b.append('}');
        String body = b.toString();

        Map<String, String> headers = new LinkedHashMap<>();
        headers.put("Content-Type", "application/json");
        // The HMAC is over exactly the bytes that are sent, which is what the hub verifies.
        headers.put("X-Hub-Signature", "sha256=" + hmac(o.secret, body.getBytes(StandardCharsets.UTF_8)));
        return new Prepared(o.hub + "/ingest/" + o.key + "/event", headers, body);
    }

    // Generated class names that change on every JVM start, and so must not reach the fingerprint.
    private static final Pattern LAMBDA = Pattern.compile("\\$\\$Lambda.*$");
    private static final Pattern CGLIB = Pattern.compile("\\$\\$(\\w+?)By\\w+\\$\\$[0-9a-fA-F]+");
    private static final Pattern PROXY = Pattern.compile("\\$Proxy\\d+");
    private static final Pattern ACCESSOR = Pattern.compile("(GeneratedMethodAccessor|GeneratedConstructorAccessor)\\d+");

    /** A class name with the parts that vary between runs removed. Public for tests. */
    public static String cleanClass(String name) {
        String n = LAMBDA.matcher(name).replaceAll("\\$\\$Lambda");
        n = CGLIB.matcher(n).replaceAll("\\$\\$$1");
        n = PROXY.matcher(n).replaceAll("\\$Proxy");
        return ACCESSOR.matcher(n).replaceAll("$1");
    }

    private static void frames(StringBuilder b, Throwable root) {
        StackTraceElement[] trace = root.getStackTrace(); // innermost FIRST
        int from = Math.min(trace.length, MAX_FRAMES);
        b.append('[');
        // reversed, so the innermost frame is LAST, which is the order the hub expects
        for (int i = from - 1; i >= 0; i--) {
            StackTraceElement e = trace[i];
            String cls = cleanClass(e.getClassName());
            b.append("{\"module\":");
            str(b, cut(cls, MAX_FIELD));
            b.append(",\"function\":");
            str(b, cut(e.getMethodName(), MAX_FIELD));
            if (e.getFileName() != null) {
                b.append(",\"file\":");
                str(b, cut(e.getFileName(), MAX_FIELD));
            }
            if (e.getLineNumber() >= 0) {
                b.append(",\"lineno\":").append(e.getLineNumber());
            }
            b.append('}');
            if (i > 0) {
                b.append(',');
            }
        }
        b.append(']');
    }

    private static List<Throwable> causeChain(Throwable t) {
        List<Throwable> chain = new ArrayList<>();
        Set<Throwable> seen = Collections.newSetFromMap(new IdentityHashMap<Throwable, Boolean>());
        Throwable cur = t == null ? new NullPointerException("a null Throwable was reported") : t;
        while (cur != null && seen.add(cur) && chain.size() < MAX_CAUSES) {
            chain.add(cur);
            cur = cur.getCause();
        }
        return chain;
    }

    // ---- internals ----------------------------------------------------------------------------

    private static Options resolve(Options in) {
        Options o = new Options();
        o.hub = firstNonEmpty(in.hub, System.getenv("NIGHTJAR_URL"), "");
        while (o.hub.endsWith("/")) {
            o.hub = o.hub.substring(0, o.hub.length() - 1);
        }
        o.key = firstNonEmpty(in.key, System.getenv("NIGHTJAR_KEY"), "");
        o.secret = firstNonEmpty(in.secret, System.getenv("NIGHTJAR_SECRET"), "");
        o.environment = firstNonEmpty(in.environment, System.getenv("NIGHTJAR_ENV"), "production");
        o.release = firstNonEmpty(in.release, System.getenv("NIGHTJAR_RELEASE"), null);
        o.timeoutMs = in.timeoutMs;
        o.maxPerMinute = in.maxPerMinute;
        o.maxQueued = in.maxQueued;
        o.uncaughtLevel = in.uncaughtLevel;
        return o;
    }

    private static boolean configured(Options o) {
        return !o.hub.isEmpty() && !o.key.isEmpty() && !o.secret.isEmpty();
    }

    private static String firstNonEmpty(String a, String b, String fallback) {
        if (a != null && !a.isEmpty()) {
            return a;
        }
        if (b != null && !b.isEmpty()) {
            return b;
        }
        return fallback;
    }

    private static boolean allowed(Options o) {
        long now = System.nanoTime();
        synchronized (sentAt) {
            while (!sentAt.isEmpty() && now - sentAt.peekFirst() > 60_000_000_000L) {
                sentAt.pollFirst();
            }
            if (sentAt.size() >= o.maxPerMinute) {
                return false;
            }
            sentAt.addLast(now);
            return true;
        }
    }

    private static String cut(String s, int max) {
        if (s == null) {
            return "";
        }
        if (s.length() <= max) {
            return s;
        }
        int end = max;
        if (Character.isHighSurrogate(s.charAt(end - 1))) {
            end--; // do not split a character in half
        }
        return s.substring(0, end);
    }

    /** A JSON string. Control characters are escaped and a lone surrogate (invalid in UTF-8) becomes U+FFFD. */
    private static void str(StringBuilder b, String s) {
        b.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                case '\b': b.append("\\b"); break;
                case '\f': b.append("\\f"); break;
                default:
                    if (c < 0x20) {
                        b.append(String.format("\\u%04x", (int) c));
                    } else if (Character.isHighSurrogate(c)) {
                        if (i + 1 < s.length() && Character.isLowSurrogate(s.charAt(i + 1))) {
                            b.append(c).append(s.charAt(++i));
                        } else {
                            b.append('�');
                        }
                    } else if (Character.isLowSurrogate(c)) {
                        b.append('�');
                    } else {
                        b.append(c);
                    }
            }
        }
        b.append('"');
    }

    private static String hmac(String secret, byte[] data) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            byte[] raw = mac.doFinal(data);
            char[] hex = new char[raw.length * 2];
            final char[] digits = "0123456789abcdef".toCharArray();
            for (int i = 0; i < raw.length; i++) {
                hex[i * 2] = digits[(raw[i] >> 4) & 0xF];
                hex[i * 2 + 1] = digits[raw[i] & 0xF];
            }
            return new String(hex);
        } catch (Exception e) {
            throw new IllegalStateException("HmacSHA256 is not available", e);
        }
    }

    private static final class ClientHolder {
        static final HttpClient CLIENT = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(1))
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    private static void httpSend(String url, Map<String, String> headers, String body, int timeoutMs) throws Exception {
        HttpRequest.Builder req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofMillis(timeoutMs))
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
        for (Map.Entry<String, String> h : headers.entrySet()) {
            req.header(h.getKey(), h.getValue());
        }
        ClientHolder.CLIENT.send(req.build(), HttpResponse.BodyHandlers.discarding());
    }

    // ---- for tests ----------------------------------------------------------------------------

    /** Replace the network. Pass null to restore the real one. */
    public static void setTransport(Transport t) {
        transport = t == null ? Nightjar::httpSend : t;
    }

    /** Forget everything, so a test starts clean. */
    public static void reset() {
        ThreadPoolExecutor p = pool;
        if (p != null) {
            p.shutdownNow();
        }
        pool = null;
        active = null;
        handlerInstalled.set(false);
        transport = Nightjar::httpSend;
        synchronized (sentAt) {
            sentAt.clear();
        }
    }
}
