// Friendly names for the hub's response shapes. The shapes themselves are generated from the
// hub's Pydantic models (schema.d.ts, `npm run types`); never write one here by hand. If a field
// is missing, change the model on the hub and regenerate.
import type { components } from "./schema.d.ts";

type S = components["schemas"];

export type SiteHealth = S["SiteHealthOut"];
export type SiteListed = S["SiteListed"];
export type SiteOut = S["SiteOut"];
export type SiteCreated = S["SiteCreated"];
export type SiteIn = S["SiteIn"];
export type SitePatch = S["SitePatch"];
export type TestEventResult = S["TestEventOut"];
export type Issue = S["IssueOut"];
export type IssueAction = S["ActionIn"];
export type LogEntry = S["LogEntry"];
export type Rising = S["RisingOut"];
export type Ticket = S["TicketOut"];
export type Deploy = S["DeployOut"];
export type Stack = S["StackOut"];
export type MediaSnapshot = S["MediaOut"];
export type MediaPlay = S["MediaPlayOut"];
export type HomeSnapshot = S["HomeOut"];
export type Presence = S["PresenceOut"];
export type Weather = S["WeatherOut"];
export type Rates = S["RatesOut"];
