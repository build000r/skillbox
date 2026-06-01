# OSS Dev-Tool CLI Distribution Research for Skillbox

Date: 2026-06-01

Scope: distribution channels and content formats for open source, single-operator or personal dev-infra CLI tools adjacent to `skillbox`: terminal AI coding agents, Claude Code helpers, local agent workspaces, dev environment CLIs, and lightweight self-hosted operator tooling.

Method notes:

- Primary sources used: GitHub repo/API metadata, GitHub stargazer API timestamps, HN Algolia story data, GitHub awesome-list repos, Product Hunt/AlternativeTo pages, and public search result pages. Star-history pages are included as visual cross-check links, but exact star velocity below is computed from GitHub stargazer timestamps.
- Retrieval date for current star/list counts: 2026-06-01.
- Do not treat any "estimated monthly views", X engagement, GitHub traffic, or GEO/AI-search citation behavior as verified unless explicitly marked verified. Most of that data is private or auth-gated.
- Banned generic OSS marketing listicles were not used.

## Star-Velocity Case Studies

Star velocity is computed from GitHub's stargazer API by taking the date of stargazer #50 and #500, then calculating the 450-star delta over elapsed days. Current star counts come from the GitHub repo API. This is a reproducible launch-velocity proxy, not GitHub's private traffic data.

| Project | Stars then -> now | Growth period | Primary channel | Content trigger | Stars/week peak | Source |
|---|---:|---|---|---|---:|---|
| `charmbracelet/crush` | 50 -> 24,897 | 2025-07-29 to 2025-07-30 for #50 -> #500 | HN plus Charm/GitHub launch audience | Terminal coding agent with strong visual/demo framing | ~3,150 | GitHub: https://github.com/charmbracelet/crush; HN story 44736176: https://hn.algolia.com/api/v1/items/44736176 |
| `dagger/container-use` | 50 -> 3,808 | 2025-06-05 to 2025-06-07 for #50 -> #500 | Dagger/GitHub audience; HN was not the driver | Concrete "isolated parallel coding agents" README/demo; later Dagger blog amplified it | ~1,575 | GitHub: https://github.com/dagger/container-use; Dagger post: https://dagger.io/blog/agent-container-use; HN story 44193565: https://hn.algolia.com/api/v1/items/44193565 |
| `anomalyco/opencode` | 50 -> 168,290 | 2025-05-14 to 2025-05-22 for #50 -> #500 | Initial channel not verified; later HN and terminal-agent discourse amplified it | "Open source coding agent" with direct Claude Code alternative positioning | ~394 | GitHub: https://github.com/anomalyco/opencode; HN story 44482504: https://hn.algolia.com/api/v1/items/44482504 |
| `smtg-ai/claude-squad` | 50 -> 7,687 | 2025-04-04 to 2025-04-18 for #50 -> #500 | Not verified; HN post was too small to explain growth alone | Multiplex multiple Claude Code/Codex/OpenCode terminal agents | ~225 | GitHub: https://github.com/smtg-ai/claude-squad; HN story 43575127: https://hn.algolia.com/api/v1/items/43575127 |
| `musistudio/claude-code-router` | 50 -> 34,617 | 2025-02-28 to 2025-06-16 for #50 -> #500 | Slow GitHub/community discovery; HN came later | Cost/control framing for Claude Code routing | ~29 | GitHub: https://github.com/musistudio/claude-code-router; HN story 44705958: https://hn.algolia.com/api/v1/items/44705958 |

Visual cross-checks: https://www.star-history.com/#charmbracelet/crush&dagger/container-use&anomalyco/opencode&smtg-ai/claude-squad&musistudio/claude-code-router&Date

Confidence:

- High for current stars, #50/#500 dates, and stars/week calculations.
- Medium for channel attribution when the HN date lines up with the growth window.
- Low when the growth preceded the visible HN event or when the relevant social channel was not publicly measurable.

## Show HN Effectiveness

HN works best here when the post title names a specific developer pain and the linked page is immediately inspectable. The strongest category matches were not generic "AI tool" claims. They were memory, usage limits, local-first execution, or concrete coding-agent workflow fixes.

| Post title | Points | Comments | Stated traffic/star impact | Key success factors |
|---|---:|---:|---|---|
| Show HN: Plandex - an AI coding engine for complex tasks | 304 | 111 | Not stated in HN/Algolia; current repo has 15,433 stars | Clear OSS repo link, complex-task positioning, direct developer workflow demo. Source: https://hn.algolia.com/api/v1/items/39918500; repo: https://github.com/plandex-ai/plandex |
| Show HN: Claude Code Usage Monitor - real-time tracker to dodge usage cut-offs | 245 | 135 | Not stated; current repo has 8,115 stars | Narrow pain, urgent utility, easy install, Claude Code adjacency. Source: https://hn.algolia.com/api/v1/items/44317012; repo: https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor |
| Show HN: Local-First Linux MicroVMs for macOS | 213 | 66 | Not stated | Local-first infra framing, low-level dev-environment appeal, concrete product page. Source: https://hn.algolia.com/api/v1/items/47113567 |
| Show HN: Stop Claude Code from forgetting everything | 202 | 226 | Not stated; current repo has 420 stars | Names the exact agent-memory pain; high comment ratio suggests strong discussion even with modest current stars. Source: https://hn.algolia.com/api/v1/items/46426624; repo: https://github.com/mutable-state-inc/ensue-skill |
| Show HN: Codemcp - Claude Code for Claude Pro subscribers - ditch API bills | 172 | 36 | Not stated; current repo has 1,613 stars | Cost-saving angle plus Claude Code integration. Source: https://hn.algolia.com/api/v1/items/43356016; repo: https://github.com/ezyang/codemcp |
| Show HN: Rudel - Claude Code Session Analytics | 144 | 86 | Not stated; current repo has 282 stars | Observable session analytics, concrete Claude Code workflow wedge. Source: https://hn.algolia.com/api/v1/items/47350416; repo: https://github.com/obsessiondb/rudel |

Adjacent non-Show-HN benchmark posts also matter. `charmbracelet/crush` reached 367 points and 235 comments on HN, and `opencode` reached 319 points and 91 comments, despite not being titled "Show HN" in Algolia. Sources: https://hn.algolia.com/api/v1/items/44736176 and https://hn.algolia.com/api/v1/items/44482504.

Synthesis:

For skillbox, HN is worth one carefully prepared launch attempt, but only after the README can answer "why this instead of DevPod/Coder/Gitpod/a raw VPS?" in the first screen. The title should be pain-first, not brand-first. The strongest candidate title shape is: "Show HN: Skillbox - a private Tailscale dev box for Claude Code and Codex agents".

HN comments are likely to challenge security, "why not devcontainers", "why not Coder", licensing, and operational complexity. Pre-answering those in README and docs is part of the launch asset, not post-launch cleanup.

## Awesome-List / Curator Map

GitHub traffic from awesome-list inclusions is not public. The "estimated monthly views" column is therefore marked not public. Stars are included only as a rough visibility proxy, not a traffic proxy.

| List name | Estimated monthly views | Stars impact per inclusion | Relevance to skillbox | Open submissions? |
|---|---|---|---|---|
| `hesreallyhim/awesome-claude-code` | Not public; 45,392 repo stars | Not public; likely discoverability/backlink only unless featured high in category | High: Claude Code workflows, hooks, skills, orchestrators, agent tooling | Not clearly verified in README during this pass. Source: https://github.com/hesreallyhim/awesome-claude-code |
| `bradAGI/awesome-cli-coding-agents` | Not public; 481 repo stars | Not public; niche list so likely low raw reach but high intent | High: terminal-native coding agents and harnesses | Yes, README shows PRs welcome. Source: https://github.com/bradAGI/awesome-cli-coding-agents |
| `jqueryscript/awesome-claude-code` | Not public; 401 repo stars | Not public | High: Claude Code tools, integrations, resources | Yes, README has contribution guidelines. Source: https://github.com/jqueryscript/awesome-claude-code |
| `jondot/awesome-devenv` | Not public; 3,267 repo stars | Not public | Medium-high: dev environment tooling, but less agent-specific | Yes, CONTRIBUTING says submit a pull request. Source: https://github.com/jondot/awesome-devenv |
| `awesome-selfhosted/awesome-selfhosted` | Not public; 296,644 repo stars | Not public; potentially high if accepted, but category fit is weaker | Medium-low unless skillbox is framed as self-hosted developer infrastructure with a usable service surface | Yes, contribution process exists. Source: https://github.com/awesome-selfhosted/awesome-selfhosted |
| `punkpeye/awesome-mcp-servers` | Not public; 88,301 repo stars | Not public | Medium only for the MCP/operator surfaces, not the whole product | Yes, CONTRIBUTING welcomes additions. Source: https://github.com/punkpeye/awesome-mcp-servers |

Recommended submission order:

1. `awesome-cli-coding-agents` after the README has a short "agent workstation" positioning line.
2. `awesome-claude-code` lists after a Claude Code-specific quickstart exists.
3. `awesome-devenv` after a one-command "private dev box" install is verified.
4. Defer `awesome-selfhosted` unless skillbox has a stable self-hosted app/service description and license.

Confidence: Medium for relevance; low for star impact.

## X Influencer Map

Primary X metrics were not verified. Public X pages loaded without useful unauthenticated engagement/follower data in this environment, and search snippets are not a reliable primary source for engagement. Treat the table as a candidate map for manual/authenticated follow-up, not as proof of reach.

| Account | Followers | Niche | Best-performing content format | Engagement benchmarks |
|---|---|---|---|---|
| `@sst_dev` / opencode ecosystem | Not verified from primary X | Terminal AI coding agent users, OpenCode launch audience | Short launch demo, direct Claude Code alternative framing, GitHub repo link | X not verified; HN proxy: opencode HN story had 319 points / 91 comments. Source: https://hn.algolia.com/api/v1/items/44482504 |
| `@charmcli` | Not verified from primary X | CLI/TUI developers, terminal UX, Go tooling | Polished terminal demo GIF/video plus install command | X not verified; HN proxy: Crush HN story had 367 points / 235 comments. Source: https://hn.algolia.com/api/v1/items/44736176 |
| `@dagger_io` | Not verified from primary X | Containers, CI/CD, agent execution environments | Problem-solution blog post plus concrete commands | X not verified; HN proxy was weak, so distribution likely came from Dagger-owned audience/GitHub. Source: https://dagger.io/blog/agent-container-use |
| `@paulgauthier` / Aider ecosystem | Not verified from primary X | AI pair programming CLI users | Frequent concrete demos, changelog posts, examples | X not verified; repo as durable source: https://github.com/Aider-AI/aider |
| `@swyx` and similar AI-engineer curators | Not verified from primary X | AI engineers, agent workflow early adopters | Curated tool roundups, opinionated workflow threads | X not verified; use only after manual engagement check in X search |

Conclusion: do not make X the main 30-day bet without verified examples. Use X as syndication for the HN/blog asset, and manually ask 3-5 relevant practitioners to try the repo rather than chasing broad influencer posting.

## Comparison Page / SEO Viability

Comparison/alternative pages exist for adjacent dev-environment tools, but the evidence for meaningful early adoption is weak. AlternativeTo has pages for Jetify Devbox and DevPod; the Devbox page listed only small visible like counts in the search extract, while Product Hunt's DevPod launch shows 121 upvotes and 14 comments. Sources: https://alternativeto.net/software/jetify-devbox/, https://alternativeto.net/software/devpod/, https://www.hunted.space/product/devpod-2/launches/devpod-2.

For skillbox, comparison SEO is still worth doing, but not because it will rank quickly for broad "Coder alternative" or "Gitpod alternative" terms. The realistic play is long-tail clarification: "skillbox vs raw VPS", "skillbox vs DevPod", "skillbox vs Coder for solo Claude Code/Codex users", and "private Tailscale agent workstation". Those pages can also serve HN commenters and awesome-list reviewers.

Priority: write one honest comparison page before Show HN, but do not spend the first 30 days building a full SEO cluster. Domain authority constraints mean comparison pages are a support asset first, acquisition asset second.

Confidence: Medium.

## Build-in-Public Playbook

1. Pain-first launch post with a reproducible demo. Confidence: High.
   Evidence: Plandex, Claude Code Usage Monitor, Codemcp, and "Stop Claude Code from forgetting everything" all performed on HN by naming a painful workflow problem and linking to usable artifacts. Sources above.

2. "My private agent workstation setup" technical blog, cross-posted to HN only when the quickstart is already clean. Confidence: High.
   Evidence: HN rewards concrete setups and will interrogate operational details. The skillbox ICP already maps to durable homes, Tailscale SSH, Docker workspace state, and agent context. This is more plausible than a generic launch page.

3. Awesome-list PR packet with a one-line category fit, install command, and comparison note. Confidence: Medium.
   Evidence: relevant curated lists exist and accept PRs, but impact is not public. This is low effort and high intent even if raw traffic is small.

4. Short terminal walkthrough video/GIF embedded in README and blog. Confidence: Medium-low.
   Evidence: HN posts with concrete demos do well, but the Dagger "video" HN item itself only had 1 point. Use video as proof inside the page, not as the standalone channel.

5. Weekly changelog/build-in-public thread. Confidence: Low.
   Evidence: no primary source in this pass showed changelog-only posting driving the 50 -> 500 star jump. Changelogs are useful after initial adoption, not as the first distribution wedge.

## GEO / AI-Search Opportunity

I could not verify ChatGPT or Perplexity citation behavior from primary logged-in surfaces in this pass. The "current top cited source" column below means current public web-search top result observed through the available search tool, not confirmed AI Overview or Perplexity citation behavior.

| Query string | Current top cited/source observed | Content gap | Recommended content format |
|---|---|---|---|
| `private coding agent workstation` | ClauBoard appeared high for "Visual Control Plane for AI Coding Agents": https://clauboard.dev/ | Results skew to dashboards/control planes, not one private Tailnet workstation | Blog post: "A private Tailscale workstation for Claude Code and Codex" |
| `self hosted dev environment for AI agents` | Aode, CrashAgents, Airut, ClaudeNest, Glade, Sesame appeared in current results | Results skew hosted/session platforms; durable single-operator box is underexplained | Comparison page: "self-hosted AI agent workspace vs private dev box" |
| `Claude Code home directory setup` | Official Claude Code docs and help center dominate: https://code.claude.com/docs/en/claude-directory and https://support.claude.com/en/articles/14553240-give-claude-context-claude-md-and-better-prompts | Official docs explain `.claude`, but not remote durable home mounting across agents | Docs page: "Durable Claude/Codex homes on a private box" |
| `Claude Code persistent context across sessions` | Official Claude docs plus community memory guides/reddit threads | Lots of memory content; little about preserving agent homes, logs, repo state, and runtime graph together | Blog post with diagram: "Persistent context is more than CLAUDE.md" |
| `Tailscale dev box coding agents` | Results were sparse/noisy; Tailscale/docker and self-hosted agent pages appear separately | Strong gap for Tailnet-first agent workstation language | Landing/docs page section: "Tailscale-first access model" |
| `private Claude Code server` | Results skew to hosted/self-hosted orchestration products | Security and private-access concerns are prominent but not tied to a thin Docker/Tailscale setup | Comparison page: "private Claude Code server without a hosted control plane" |
| `DevPod alternative for AI coding agents` | AlternativeTo/DevPod and general CDE content show up for adjacent terms | DevPod comparisons do not focus on durable coding-agent homes and session logs | Honest comparison page: "skillbox vs DevPod" |
| `Coder alternative for solo developer AI agents` | Coder/Gitpod/CDE pages dominate adjacent terms | Solo operator, no team control plane, Tailnet-first positioning is missing | Comparison page: "skillbox vs Coder/Gitpod for solo operators" |

Confidence: Medium for public web-search gaps; low for AI-search citation behavior.

## Prioritized 30-60 Day Distribution Plan

1. Ship the "private agent workstation" launch asset and submit one Show HN.
   Rationale: HN has the clearest verified category evidence, and the best posts win by naming an exact developer pain with a runnable repo. Confidence: High.

2. Submit focused awesome-list PRs after the launch page is live.
   Rationale: Low effort, high-intent backlinks/discovery, and the relevant Claude Code/CLI-agent/devenv lists exist, but impact is not public. Confidence: Medium.

3. Publish one honest comparison page: "skillbox vs raw VPS vs DevPod vs Coder for solo Claude Code/Codex users".
   Rationale: It pre-answers objections from HN/list reviewers and targets long-tail search/GEO gaps without pretending skillbox can outrank broad CDE terms immediately. Confidence: Medium.

Evidence gaps to keep open:

- No verified GitHub traffic sources, clone/install counts, or awesome-list referral data.
- No verified X follower/engagement data from primary X surfaces.
- No verified ChatGPT/Perplexity/Google AI Overview citation behavior.
- Star velocity is computed from public GitHub stargazer timestamps, which is reliable for stars but not for causality.
