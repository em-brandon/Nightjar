package nightjar;

import com.sun.net.httpserver.HttpServer;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * The Java reporter's rules. No test framework, on purpose: the reporter has no dependencies and neither does this.
 *
 *   javac --release 11 -d out client/java/Nightjar.java client/java/NightjarTest.java
 *   java -cp out nightjar.NightjarTest
 *
 * Run it with NIGHTJAR_* unset, because one test checks that an unconfigured reporter does nothing.
 */
public final class NightjarTest {
    static int failures = 0;

    static void check(String name, boolean ok) {
        check(name, ok, "");
    }

    static void check(String name, boolean ok, String detail) {
        if (ok) {
            System.out.println("  ok   " + name);
        } else {
            failures++;
            System.out.println("  FAIL " + name + (detail.isEmpty() ? "" : " -- " + detail));
        }
    }

    static Nightjar.Options opts() {
        return new Nightjar.Options().hub("https://nightjar.example/").key("KEY1").secret("s3cret").release("v2");
    }

    static String hex(byte[] raw) {
        StringBuilder b = new StringBuilder();
        for (byte x : raw) {
            b.append(String.format("%02x", x));
        }
        return b.toString();
    }

    static String hmac(String secret, String body) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        return hex(mac.doFinal(body.getBytes(StandardCharsets.UTF_8)));
    }

    // two real methods, so the stack has something to say
    static Exception deepB(int n) {
        return new IllegalArgumentException("bad value " + n);
    }

    static Exception deepA() {
        return deepB(42);
    }

    static final class Cart {
        Exception total() {
            return new IllegalStateException("cart is empty");
        }
    }

    public static void main(String[] args) throws Exception {
        // ---- the signed request ---------------------------------------------------------
        Nightjar.Prepared p = Nightjar.prepare(opts(), deepA(), Nightjar.Level.ERROR, "POST", "/cart?token=SECRET");
        check("the url has the key and no double slash", p.url.equals("https://nightjar.example/ingest/KEY1/event"), p.url);
        check("the HMAC is over exactly the bytes sent", p.headers.get("X-Hub-Signature").equals("sha256=" + hmac("s3cret", p.body)));
        check("type, message, level, release, environment",
                p.body.contains("\"exception_type\":\"java.lang.IllegalArgumentException\"")
                        && p.body.contains("\"message\":\"bad value 42\"")
                        && p.body.contains("\"level\":\"error\"")
                        && p.body.contains("\"release\":\"v2\"")
                        && p.body.contains("\"environment\":\"production\""), p.body);
        check("the query string is never sent", !p.body.contains("SECRET") && p.body.contains("\"path\":\"/cart\""));

        // ---- frames ---------------------------------------------------------------------
        String body = p.body;
        int fa = body.indexOf("\"function\":\"deepA\"");
        int fb = body.indexOf("\"function\":\"deepB\"");
        check("the throw site is the INNERMOST frame, and is last", fb >= 0 && fa >= 0 && fb > fa, "deepA@" + fa + " deepB@" + fb);
        check("a frame has the class as its module, a file and a line",
                body.contains("\"module\":\"nightjar.NightjarTest\"") && body.contains("\"file\":\"NightjarTest.java\"") && body.contains("\"lineno\":"));

        // ---- root cause -----------------------------------------------------------------
        Exception wrapped = new RuntimeException("wrapper", new java.io.IOException("outer io", new Cart().total()));
        String w = Nightjar.prepare(opts(), wrapped, Nightjar.Level.ERROR, null, null).body;
        check("a wrapped exception is reported as its ROOT cause", w.contains("\"exception_type\":\"java.lang.IllegalStateException\"") && w.contains("\"message\":\"cart is empty\""), w);
        check("and the wrappers are listed, not lost", w.contains("\"wrapped_by\":[\"java.lang.RuntimeException: wrapper\",\"java.io.IOException: outer io\"]"), w);
        Exception selfCause = new RuntimeException("loop");
        selfCause.initCause(new RuntimeException("a", selfCause));
        check("a cause cycle does not hang", Nightjar.prepare(opts(), selfCause, Nightjar.Level.ERROR, null, null).body.length() > 0);

        // ---- names that change on every start ------------------------------------------
        check("a CGLIB proxy name loses its hash", Nightjar.cleanClass("com.shop.CartService$$EnhancerBySpringCGLIB$$3fa9c1b2").equals("com.shop.CartService$$Enhancer"), Nightjar.cleanClass("com.shop.CartService$$EnhancerBySpringCGLIB$$3fa9c1b2"));
        check("a FastClass name loses its hash", Nightjar.cleanClass("com.shop.CartService$$FastClassBySpringCGLIB$$ab12cd34").equals("com.shop.CartService$$FastClass"));
        check("a lambda loses its number and address", Nightjar.cleanClass("com.shop.Cart$$Lambda$123/0x0000000800c03000").equals("com.shop.Cart$$Lambda") && Nightjar.cleanClass("com.shop.Cart$$Lambda/0x00007f").equals("com.shop.Cart$$Lambda"));
        check("a JDK proxy and a generated accessor lose their numbers", Nightjar.cleanClass("com.sun.proxy.$Proxy42").equals("com.sun.proxy.$Proxy") && Nightjar.cleanClass("jdk.internal.reflect.GeneratedMethodAccessor17").equals("jdk.internal.reflect.GeneratedMethodAccessor"));
        check("an ordinary class is untouched", Nightjar.cleanClass("com.shop.web.CartController$Inner").equals("com.shop.web.CartController$Inner"));

        // ---- JSON escaping --------------------------------------------------------------
        String nasty = "quote\" back\\slash new\nline tab\t ctrl\u0001 emoji 😀 lone\uD800 end";
        String n = Nightjar.prepare(opts(), new RuntimeException(nasty), Nightjar.Level.ERROR, null, null).body;
        boolean noRawControls = true;
        for (char c : n.toCharArray()) {
            if (c < 0x20) {
                noRawControls = false;
            }
        }
        check("quotes, backslashes, newlines and controls are escaped", noRawControls && n.contains("quote\\\" back\\\\slash new\\nline tab\\t ctrl\\u0001"), n);
        check("an emoji survives and a lone surrogate becomes U+FFFD", n.contains("😀") && n.contains("lone� end") && !n.contains("\uD800"));
        String big = Nightjar.prepare(opts(), new RuntimeException("y".repeat(50000)), Nightjar.Level.ERROR, null, null).body;
        check("a huge message is cut under the hub's limit", big.length() < 8000, String.valueOf(big.length()));

        // ---- never breaking the app -----------------------------------------------------
        Nightjar.reset();
        boolean threw = false;
        try {
            Nightjar.captureException(new RuntimeException("before init"));
            Nightjar.captureException(null);
            Nightjar.captureException(null, null, null, null);
        } catch (Throwable t) {
            threw = true;
        }
        check("capturing before init, or null, never throws", !threw);
        check("an unconfigured init does nothing and says so", !Nightjar.init());

        List<String> sent = new CopyOnWriteArrayList<>();
        Nightjar.setTransport((url, h, b, t) -> sent.add(b));
        Nightjar.init(opts());
        Throwable hostile = new RuntimeException("x") {
            @Override public String getMessage() { throw new IllegalStateException("getter"); }
            @Override public StackTraceElement[] getStackTrace() { throw new IllegalStateException("getter"); }
            @Override public synchronized Throwable getCause() { throw new IllegalStateException("getter"); }
        };
        threw = false;
        try {
            Nightjar.captureException(hostile);
        } catch (Throwable t) {
            threw = true;
        }
        Nightjar.flush(1000);
        check("a hostile exception object never throws into the app", !threw);

        Nightjar.reset();
        Nightjar.setTransport((url, h, b, t) -> { throw new java.io.IOException("network down"); });
        Nightjar.init(opts());
        threw = false;
        try {
            Nightjar.captureException(new RuntimeException("x"));
            Nightjar.flush(1000);
        } catch (Throwable t) {
            threw = true;
        }
        check("a broken network never throws into the app", !threw);

        // ---- the brakes -----------------------------------------------------------------
        Nightjar.reset();
        Nightjar.Options rate = opts();
        rate.maxPerMinute = 3;
        sent.clear();
        Nightjar.setTransport((url, h, b, t) -> sent.add(b));
        Nightjar.init(rate);
        for (int i = 0; i < 10; i++) {
            Nightjar.captureException(new RuntimeException("e" + i));
        }
        Nightjar.flush(2000);
        check("a cap on reports per minute", sent.size() == 3, String.valueOf(sent.size()));

        Nightjar.reset();
        final CountDownLatch gate = new CountDownLatch(1);
        final List<String> delivered = new CopyOnWriteArrayList<>();
        Nightjar.setTransport((url, h, b, t) -> {
            gate.await(5, TimeUnit.SECONDS);
            delivered.add(b);
        });
        Nightjar.Options queue = opts();
        queue.maxPerMinute = 1000;
        Nightjar.init(queue);
        long t0 = System.nanoTime();
        for (int i = 0; i < 40; i++) {
            Nightjar.captureException(new RuntimeException("e" + i));
        }
        long ms = (System.nanoTime() - t0) / 1_000_000;
        check("with the hub hung, the app is never made to wait", ms < 500, ms + " ms");
        gate.countDown();
        Nightjar.flush(3000);
        check("and only a few reports pile up: one sending plus a queue of five, the rest dropped", delivered.size() >= 1 && delivered.size() <= 6, String.valueOf(delivered.size()));

        // ---- the uncaught-exception handler ---------------------------------------------
        Nightjar.reset();
        Thread.UncaughtExceptionHandler original = Thread.getDefaultUncaughtExceptionHandler();
        final List<String> previousCalls = new ArrayList<>();
        Thread.setDefaultUncaughtExceptionHandler((thr, err) -> previousCalls.add(err.getMessage()));
        sent.clear();
        Nightjar.setTransport((url, h, b, t) -> sent.add(b));
        Nightjar.init(opts());
        Nightjar.installUncaughtHandler();
        Nightjar.installUncaughtHandler();
        Nightjar.installUncaughtHandler();
        Thread.getDefaultUncaughtExceptionHandler().uncaughtException(Thread.currentThread(), new RuntimeException("a thread died"));
        check("a dying thread is reported before the handler returns", sent.size() == 1 && sent.get(0).contains("a thread died") && sent.get(0).contains("\"level\":\"error\""), sent.toString());
        check("and the handler that was there before still runs, once, even after three installs", previousCalls.equals(Collections.singletonList("a thread died")), previousCalls.toString());
        Thread.setDefaultUncaughtExceptionHandler(original);

        // ---- the real network -----------------------------------------------------------
        Nightjar.reset();
        final List<String> got = new CopyOnWriteArrayList<>();
        final List<String> sigs = new CopyOnWriteArrayList<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", ex -> {
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            try (InputStream in = ex.getRequestBody()) {
                in.transferTo(buf);
            }
            got.add(ex.getRequestURI().getPath() + "\n" + buf.toString(StandardCharsets.UTF_8));
            sigs.add(ex.getRequestHeaders().getFirst("X-Hub-Signature"));
            ex.sendResponseHeaders(200, 2);
            ex.getResponseBody().write("{}".getBytes());
            ex.close();
        });
        server.start();
        try {
            Nightjar.init(opts().hub("http://127.0.0.1:" + server.getAddress().getPort()));
            Nightjar.captureException(new IllegalStateException("over a real socket 😀"), Nightjar.Level.ERROR, "GET", "/orders/7?x=1");
            Nightjar.flush(3000);
            check("a report reaches a real server", got.size() == 1, got.toString());
            if (got.size() == 1) {
                String[] parts = got.get(0).split("\n", 2);
                check("at the right path", parts[0].equals("/ingest/KEY1/event"), parts[0]);
                check("and the signature matches the bytes that arrived", sigs.get(0).equals("sha256=" + hmac("s3cret", parts[1])), sigs.get(0));
                check("including a multi-byte character", parts[1].contains("😀"));
            }
        } finally {
            server.stop(0);
        }

        Nightjar.reset();
        Nightjar.init(opts().hub("http://127.0.0.1:9"));
        long s = System.nanoTime();
        Nightjar.captureException(new RuntimeException("hub is down"));
        long captured = (System.nanoTime() - s) / 1_000_000;
        Nightjar.flush(4000);
        long total = (System.nanoTime() - s) / 1_000_000;
        check("with the hub down, capturing returns at once", captured < 300, captured + " ms");
        check("and the failed send is over within the timeout", total < 4500, total + " ms");

        // ---- a hub that is not https -----------------------------------------------------------------------------
        for (String good : new String[] {"https://nightjar.example", "https://nightjar.example/", "http://localhost:8000", "http://127.0.0.1:9", "http://[::1]:8000", "http://app.localhost"}) {
            check(good + " is a usable hub", Nightjar.hubRefusal(good) == null);
        }
        for (String bad : new String[] {"http://nightjar.example", "http://10.0.0.5", "http://192.168.1.20:8000", "http://hub.test", "ftp://nightjar.example", "nightjar.example", "//nightjar.example"}) {
            check(bad + " is refused", Nightjar.hubRefusal(bad) != null);
        }
        String why = String.valueOf(Nightjar.hubRefusal("http://user:pa55word@nightjar.example/path?token=abc"));
        check("the reason names the host but never the whole address", !why.contains("pa55word") && !why.contains("token=abc") && !why.contains("user:"));

        Nightjar.reset();
        List<String> logged = new CopyOnWriteArrayList<>();
        Nightjar.setLogger(logged::add);
        List<String> sentToBadHub = new CopyOnWriteArrayList<>();
        Nightjar.setTransport((u1, h1, b1, t1) -> sentToBadHub.add(u1));
        boolean refusedThrew = false;
        boolean refusedConfigured = true;
        try {
            refusedConfigured = Nightjar.init(opts().hub("http://nightjar.example"));
            Nightjar.captureException(new RuntimeException("boom"));
            Nightjar.init(opts().hub("http://nightjar.example"));
            Nightjar.captureException(new RuntimeException("boom again"));
            Nightjar.flush(500);
        } catch (Throwable t) {
            refusedThrew = true;
        }
        check("an http:// hub throws nothing", !refusedThrew);
        check("init says it is not configured", !refusedConfigured);
        check("it says so once, on standard error, not silently", logged.size() == 1 && logged.get(0).contains("refusing to report errors to http://nightjar.example"), logged.toString());
        check("and nothing is sent", sentToBadHub.isEmpty());

        for (String hub : new String[] {"https://nightjar.example", "http://127.0.0.1:8000", "http://localhost:8000"}) {
            Nightjar.reset();
            List<String> quiet = new CopyOnWriteArrayList<>();
            List<String> sentOk = new CopyOnWriteArrayList<>();
            Nightjar.setLogger(quiet::add);
            Nightjar.setTransport((u2, h2, b2, t2) -> sentOk.add(u2));
            Nightjar.init(opts().hub(hub));
            Nightjar.captureException(new RuntimeException("boom"));
            Nightjar.flush(2000);
            check(hub + " still reports", sentOk.size() == 1, sentOk.toString());
            check(hub + " is not warned about", quiet.isEmpty());
        }

        Nightjar.reset();
        System.out.println(failures == 0 ? "\njava reporter passed" : "\n" + failures + " FAILED");
        System.exit(failures == 0 ? 0 : 1);
    }
}
