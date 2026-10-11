// The one object App builds from every poll and hands to each view as `f`. Typed in one place so a
// view that reads a feed the shell does not provide, or reads it as the wrong shape, fails to compile.
import type { Deploy, HomeSnapshot, Issue, MediaPlay, MediaSnapshot, Presence, Rates, Rising, SiteHealth, Stack, Ticket, Weather } from "./api/types.ts";
import type { DemoHome } from "./home.ts";
import type { DemoMedia } from "./media.ts";

export type TabId = "overview" | "incidents" | "requests" | "deploys" | "staging" | "media" | "sites" | "idle";

export type Feeds = {
  sites: SiteHealth[];
  issues: Issue[];
  rising: Rising[];
  tickets: Ticket[];
  deploys: Deploy[];
  stacks: Stack[];
  media: MediaSnapshot[];
  mediaHistory: MediaPlay[];
  // decoration: absent until the feed answers, and never makes the hub look unreachable
  weather: Weather | null;
  home: HomeSnapshot[];
  // sample data, tagged, only once the feed has answered and is empty
  homeDemo: DemoHome[] | null;
  presence: Presence | null;
  rates: Rates | null;
  mediaDemo: DemoMedia[] | null;
  reloadPresence: () => Promise<void>;
  now: number;
  go: (tab: TabId) => void;
  // A screen holding something that cannot be recovered (a half-filled form, a secret shown once) asks the
  // shell not to start the idle screen over it. Always released when that screen goes away.
  setIdleHold: (hold: boolean) => void;
  hubError: boolean;
  loaded: boolean;
  updatedAt: number | null;
  reloadIssues: () => Promise<unknown>;
  reloadTickets: () => Promise<void>;
  reloadStacks: () => Promise<void>;
};
