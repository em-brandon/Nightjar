// "Mozilla/5.0 (Macintosh; ...) ... Chrome/130 Safari/537" is not something to read; name the browser and the system.
export function deviceName(userAgent: string): string {
  if (!userAgent) return "Unknown device";
  const browser = /Edg\//.test(userAgent) ? "Edge" : /Firefox\//.test(userAgent) ? "Firefox" : /Chrome\//.test(userAgent) ? "Chrome" : /Safari\//.test(userAgent) ? "Safari" : "Browser";
  const system = /iPhone|iPad/.test(userAgent) ? "iOS" : /Android/.test(userAgent) ? "Android" : /Windows/.test(userAgent) ? "Windows" : /Mac OS X|Macintosh/.test(userAgent) ? "macOS" : /Linux/.test(userAgent) ? "Linux" : "";
  return system ? `${browser} on ${system}` : browser;
}
