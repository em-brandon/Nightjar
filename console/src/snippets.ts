// Setup snippets for the onboarding screen: what to paste into a client's app, with the site's own
// values already filled in. Pure, so the wording is tested without a browser and there is one place to
// change when a reporter's setup changes (docs/REPORTERS.md is the source of truth for these).

export type StackId = "python" | "node" | "php" | "java" | "browser";

export const STACKS: { id: StackId; label: string }[] = [
  { id: "python", label: "Python (FastAPI)" },
  { id: "node", label: "Node (Express)" },
  { id: "php", label: "PHP / Laravel / WordPress" },
  { id: "java", label: "Java (Spring)" },
  { id: "browser", label: "Web page" },
];

export type SnippetInput = {
  stack: StackId;
  // the hub's address, for example https://nightjar.example.co.ke
  hub: string;
  ingestKey: string;
  // only known in the create or rotate response; after that the snippet shows a placeholder
  ingestSecret?: string | null;
  // browser reporting is off until the site lists the pages allowed to report
  publicKey?: string | null;
};

export type Snippet = {
  // the environment variables, which is also what to store as secrets in Gitea
  env: string;
  code: string;
  filename: string;
  notes: string[];
};

export const SECRET_PLACEHOLDER = "<ingest secret>";
export const PUBLIC_KEY_PLACEHOLDER = "<public key>";

// Names a CI secret store should hold them under, so a rotation is one edit (docs/CLIENT-INTEGRATION.md).
export const SECRET_NAMES = ["NIGHTJAR_URL", "NIGHTJAR_KEY", "NIGHTJAR_SECRET"] as const;

const trimHub = (hub: string) => hub.trim().replace(/\/+$/, "");

export function envBlock(hub: string, ingestKey: string, ingestSecret?: string | null): string {
  return [
    `NIGHTJAR_URL=${trimHub(hub)}`,
    `NIGHTJAR_KEY=${ingestKey}`,
    `NIGHTJAR_SECRET=${ingestSecret || SECRET_PLACEHOLDER}`,
  ].join("\n");
}

export function snippet(i: SnippetInput): Snippet {
  const hub = trimHub(i.hub);
  const env = envBlock(hub, i.ingestKey, i.ingestSecret);

  switch (i.stack) {
    case "python":
      return {
        env,
        filename: "main.py",
        code: [
          "import os",
          "from reporter import install_reporter  # copy client/reporter.py into the app",
          "",
          `install_reporter(app, url="${hub}/ingest/${i.ingestKey}/event", secret=os.environ["NIGHTJAR_SECRET"])`,
        ].join("\n"),
        notes: [
          "The Python reporter takes the full event URL; the others take the hub address and the key separately.",
          "Only NIGHTJAR_SECRET needs to be in the environment. The key is not secret.",
        ],
      };
    case "node":
      return {
        env,
        filename: "server.ts",
        code: [
          'import { init, expressErrorHandler, installProcessHandlers } from "./nightjar";',
          "",
          "init();                          // reads NIGHTJAR_URL, NIGHTJAR_KEY, NIGHTJAR_SECRET",
          "installProcessHandlers();        // report a crash that kills the process",
          "// ... your routes ...",
          "app.use(expressErrorHandler());  // after the routes, before your own error handler",
        ].join("\n"),
        notes: ["Copy client/node/nightjar.ts into the app. Node 22.18 or newer runs it directly."],
      };
    case "php":
      return {
        env,
        filename: "bootstrap.php",
        code: [
          "require __DIR__ . '/Nightjar.php';",
          "\Nightjar\Reporter::install([",
          `    'hub'    => '${hub}',`,
          `    'key'    => '${i.ingestKey}',`,
          "    'secret' => getenv('NIGHTJAR_SECRET'),",
          "]);",
        ].join("\n"),
        notes: [
          "Under PHP-FPM getenv() is empty unless the pool sets clear_env = no or lists the variable. Pass the secret from your framework's config if in doubt.",
          "Call install() after your app registers its own exception handler.",
        ],
      };
    case "java":
      return {
        env,
        filename: "Application.java",
        code: [
          "Nightjar.init();                    // reads NIGHTJAR_URL, NIGHTJAR_KEY, NIGHTJAR_SECRET",
          "Nightjar.installUncaughtHandler();  // report a thread that dies",
        ].join("\n"),
        notes: ["Copy client/java/Nightjar.java into the app. Java 11 or newer."],
      };
    case "browser": {
      const key = i.publicKey || PUBLIC_KEY_PLACEHOLDER;
      const notes = [
        "A page cannot keep a secret, so it uses the site's separate public key. That key can only report errors, and only from the pages listed for this site.",
      ];
      if (!i.publicKey) notes.unshift("Browser reporting is off. List the pages allowed to report for this site first, and a public key will appear.");
      return {
        // the secret is deliberately absent: a page must never be given it
        env: "",
        filename: "main.ts",
        code: [
          'import { init } from "./nightjar";',
          "",
          `init({ hub: "${hub}", key: "${key}" });`,
        ].join("\n"),
        notes,
      };
    }
  }
}
