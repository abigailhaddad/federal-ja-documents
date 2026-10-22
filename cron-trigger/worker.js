// Starts the "Daily J&A collection" workflow on time (2026-10-22).
//
// GitHub's own schedule for daily.yml (cron 15 6 * * *) fired 5-7 hours late
// every day of the week of 2026-10-19, so the 7:00 AM ET digest went out late
// or not at all. Cloudflare's cron triggers run on time, so this Worker calls
// GitHub's workflow_dispatch API at 06:15 UTC instead. The rest of the chain is
// unchanged: collection -> extraction (workflow_run) -> digest (workflow_run).
//
// Secret: GITHUB_TOKEN, a fine-grained token for this one repository with
// "Actions: Read and write" and nothing else. Set it with
//   npx wrangler secret put GITHUB_TOKEN
const REPO = "abigailhaddad/federal-ja-documents";
const WORKFLOW = "daily.yml";

async function dispatch(env) {
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "federal-ja-cron-trigger",
    },
    // the same inputs the scheduled run uses (publish to Hugging Face)
    body: JSON.stringify({ ref: "main", inputs: { publish: "true", archives: "auto", max_usd: "2" } }),
  });
  if (r.status !== 204) {
    // a thrown error marks the cron invocation failed in the Cloudflare dashboard
    throw new Error(`workflow_dispatch returned ${r.status}: ${await r.text()}`);
  }
  console.log(`dispatched ${WORKFLOW} on ${REPO}`);
}

export default {
  async scheduled(event, env, ctx) {
    await dispatch(env);
  },
};
