import { AppWindow, Building2, FlaskConical } from "lucide-react";
import type { SiteHealth } from "./api/types.ts";
import { groupByClient, worstState } from "./grouping.ts";
import { plural } from "./hooks.ts";
import { Dot, SiteCard } from "./ui.tsx";

// The Overview's site checks, one card per client. Each production site sits beside its staging site, so one
// glance answers "how is this client doing, and is what is on staging ready?". The card's dot is its worst
// site, so a client with something wrong is never hidden behind a healthy-looking neighbour.
export default function ClientCards({ sites, now }: { sites: SiteHealth[]; now: number }) {
  return (
    <div className="client-list">
      {groupByClient(sites).map((c) => {
        const stagingCount = c.sites.filter((s) => s.env === "staging").length;
        const productionCount = c.sites.length - stagingCount;
        return (
          <section key={c.client} className="card client-card" aria-label={c.client}>
            <header className="client-head">
              <span className="client-icon">
                <Building2 size={18} aria-hidden="true" />
              </span>
              <h3>{c.client}</h3>
              <span className="client-meta">
                {plural(productionCount, "site", "sites")}
                {stagingCount > 0 && (
                  <>
                    {" · "}
                    <FlaskConical size={13} aria-hidden="true" /> {stagingCount} staging
                  </>
                )}
              </span>
              <Dot state={worstState(c.sites)} />
            </header>
            <div className="client-groups">
              {c.groups.map((g) => (
                <div key={g.key} className="site-group">
                  <h4 className="group-title">
                    <AppWindow size={15} aria-hidden="true" />
                    {g.key}
                  </h4>
                  <div className={g.production && g.staging ? "site-pair" : "site-single"}>
                    {g.production && <SiteCard site={g.production} now={now} tier="Production" />}
                    {g.staging && <SiteCard site={g.staging} now={now} tier="Staging" />}
                  </div>
                </div>
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}
