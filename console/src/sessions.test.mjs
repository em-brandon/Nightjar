// The device label shown under Signed-in devices. Run with: npm test

import assert from "node:assert/strict";
import { deviceName } from "./sessions.ts";

const chromeMac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
const safariIphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";
const edgeWin = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0";
const firefoxLinux = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0";

assert.equal(deviceName(chromeMac), "Chrome on macOS");
assert.equal(deviceName(safariIphone), "Safari on iOS");
assert.equal(deviceName(edgeWin), "Edge on Windows", "Edge also says Chrome and Safari; it must win");
assert.equal(deviceName(firefoxLinux), "Firefox on Linux");
assert.equal(deviceName(""), "Unknown device");
assert.equal(deviceName("curl/8.0"), "Browser");
console.log("  ok   a device is named by browser and system, not by its user agent string");
