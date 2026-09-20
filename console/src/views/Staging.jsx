import { plural } from "../hooks.js";
import { Empty, Odometer, SiteCard } from "../ui.jsx";
import { DeployHowTo, DeployList } from "./Deploys.jsx";

export default function Staging({ f }) {
  const { sites, deploys, now } = f;
  const staging = sites.filter((s) => s.env === "staging");
  const stagingDeploys = deploys.filter((d) => d.env === "staging");
  const up = staging.filter((s) => s.state === "up").length;

  return (
    <>
      <div className="page-head">
        <h1>Staging</h1>
        <span className="count">
          <Odometer value={up} /> of {plural(staging.length, "environment", "environments")} up
        </span>
      </div>

      {staging.length === 0 ? (
        <Empty>
          No staging environment yet. A staging environment is just a site with env{" "}
          <code>staging</code> and a <code>url</code>, so it gets the same checks as production.
        </Empty>
      ) : (
        <div className="site-grid">
          {staging.map((s) => (
            <SiteCard key={s.id} site={s} now={now} />
          ))}
        </div>
      )}

      <div className="sec">
        <h2>Staging deploys</h2>
        <span className="count">{stagingDeploys.length} recorded</span>
      </div>
      {stagingDeploys.length === 0 ? (
        <DeployHowTo env="staging" />
      ) : (
        <DeployList deploys={stagingDeploys} now={now} />
      )}
    </>
  );
}
