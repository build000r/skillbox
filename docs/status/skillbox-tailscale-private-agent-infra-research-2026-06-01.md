# Skillbox Tailscale Private Agent Infrastructure Research - 2026-06-01

Scope: evidence brief for `skillbox-deep-research-tailscale-private-agent-infra-gpd`.
This report is limited to developer infrastructure, coding agents, remote
development environments, and AI-agent runtime tooling. General enterprise IT
VPN deployments, consumer VPN use, gaming, and generic homelab media access are
excluded unless they reveal developer-infra behavior.

Verdict: Tailscale remains a strong transport bet for a narrow CLI-first,
operator-owned private-box product, but the evidence does not prove a large
Tailscale-native AI-agent tooling segment yet. The managed agent runtime segment
is growing faster and has clearer AI-specific adoption signals. Skillbox should
keep Tailscale as the default opinion, make its interface transport-aware, and
avoid hard-coding roadmap value into Tailscale-only features until more direct
AI-agent adoption evidence appears.

## Executive Summary

- Tailscale has strong developer-infra signals: the main repo shows about 32.1k
  GitHub stars, Homebrew reports about 188.9k formula installs and 98.8k macOS
  cask installs over 365 days, the GitHub Action has about 900 stars, and
  high-engagement HN threads exist for Tailscale itself and Tailscale SSH
  ([tailscale/tailscale](https://github.com/tailscale/tailscale),
  [Homebrew formula API](https://formulae.brew.sh/api/formula/tailscale.json),
  [Homebrew cask API](https://formulae.brew.sh/api/cask/tailscale-app.json),
  [tailscale/github-action](https://github.com/tailscale/github-action),
  [HN Tailscale is pretty useful](https://news.ycombinator.com/item?id=43270835),
  [HN Tailscale SSH](https://news.ycombinator.com/item?id=31837115)).
- Direct evidence for "AI-agent developers choose Tailscale as their agent
  infrastructure layer" is thin. The strongest direct signal is Tailscale's own
  Aperture positioning around Claude Code, Codex, Gemini CLI, MCP and local tool
  calls, plus docs for GitHub Actions, Kubernetes, SSH, and containerized
  environments ([Aperture](https://tailscale.com/use-cases/securing-ai),
  [GitHub Actions runners](https://tailscale.com/blog/private-connections-for-github-actions),
  [Tailscale on Kubernetes](https://tailscale.com/docs/kubernetes)).
- Competing private-network tools are not absent. Headscale is especially strong
  as a self-hosted control-plane hedge at about 39.5k stars, NetBird is strong at
  about 25.7k stars, Cloudflare Tunnel is heavily adopted for public/private app
  exposure at about 14.4k cloudflared stars, and ZeroTier remains a mature
  alternative at about 16.8k stars
  ([headscale](https://github.com/juanfont/headscale),
  [netbird](https://github.com/netbirdio/netbird),
  [cloudflared](https://github.com/cloudflare/cloudflared),
  [ZeroTierOne](https://github.com/zerotier/ZeroTierOne)).
- Managed AI-agent runtimes have clearer AI-native growth signals than
  private-network tools. E2B claims 94 percent Fortune 100 usage, 3.5M+ monthly
  downloads, and 1B+ started sandboxes; Daytona shows about 72.5k GitHub stars;
  GitHub Copilot cloud agent is available across paid Copilot plans and rides on
  GitHub's 150M+ developer platform and 77k+ Copilot organizations; Cursor
  Background Agents and OpenAI Codex both expose hosted/cloud execution paths
  ([E2B](https://e2b.dev/), [Daytona docs](https://www.daytona.io/docs/en/sandboxes/),
  [GitHub Copilot cloud agent docs](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/access-management),
  [Cursor Background Agents](https://docs.cursor.com/background-agent),
  [OpenAI Codex](https://openai.com/index/introducing-codex/)).
- Decision for Skillbox: double down on the "durable private machine" wedge, but
  prioritize proof surfaces that work across transports: SSH, Tailscale,
  Headscale, WireGuard, and Cloudflare Tunnel. Defer deep Tailscale-native ACL,
  tag, and exit-node features until the product has stronger user evidence from
  Tailscale-first agent operators.

## Tailscale Adoption Evidence

### Quantitative and Semi-Quantitative Signals

| Signal | Current evidence | Interpretation for Skillbox | Confidence |
|---|---:|---|---|
| Core repo adoption | `tailscale/tailscale`: 32.1k GitHub stars, 2.6k forks, pushed on 2026-06-01 when checked ([repo](https://github.com/tailscale/tailscale)) | Strong developer awareness and active project health, but not AI-agent-specific. | High |
| CI/CD integration | `tailscale/github-action`: 900 GitHub stars, 131 forks, latest release v4.1.2 on 2026-03-11; Tailscale announced Windows and macOS support joining Linux GA on 2025-06-10 ([repo](https://github.com/tailscale/github-action), [announcement](https://tailscale.com/blog/private-connections-for-github-actions)) | Strong fit for private CI runners, deploys, private test DBs, and SSH debugging. This maps directly to agent execution and proof jobs. | High |
| macOS developer installs | Homebrew formula `tailscale` showed 188,894 installs and 188,842 installs-on-request over 365 days; cask `tailscale-app` showed 98,811 installs over 365 days, retrieved 2026-06-01 ([formula API](https://formulae.brew.sh/api/formula/tailscale.json), [cask API](https://formulae.brew.sh/api/cask/tailscale-app.json)) | Strong solo-developer and Mac developer signal, though Homebrew users include homelab and IT users too. | Medium |
| Free/personal tier capacity | Personal plan: $0, unlimited user devices, up to 6 users, up to 3 ACL groups, 50 tagged resources, 1,000 ephemeral-resource minutes/month. Standard: $8/user/month. Premium: $18/user/month with 10,000 ephemeral minutes, network flow logs, log streaming, advanced SSH ([pricing](https://tailscale.com/pricing)) | Solo and tiny-team Skillbox users fit the free tier unless they model many tagged agents/sidecars. Tagged-resource pricing can become a pain point for agent-per-container patterns. | High |
| Developer community engagement | HN "Tailscale is pretty useful" on 2025-03-05: 804 points / 404 comments; HN "Tailscale SSH" on 2022-06-22: 759 points / 303 comments; HN "Tailscale SSH is now Generally Available" on 2024-04-17: 212 points / 92 comments ([HN 43270835](https://news.ycombinator.com/item?id=43270835), [HN 31837115](https://news.ycombinator.com/item?id=31837115), [HN 40060901](https://news.ycombinator.com/item?id=40060901)) | Strong ongoing developer mindshare. The comments show the central tradeoff: convenience and private access vs trust in Tailscale identity/control-plane semantics. | Medium |
| AI-specific Tailscale positioning | Tailscale Aperture says it covers AI agents and users, supports Claude Code, Codex, Gemini CLI, Roo Code, Cline and MCP/local tool-call extraction, and works in GitHub Actions/container environments where Tailscale can run ([Aperture](https://tailscale.com/use-cases/securing-ai)) | Tailscale is actively moving toward AI-agent governance, but this is vendor positioning, not proof of grassroots agent-runtime adoption. | Medium |

### Qualitative Signals

- Tailscale's official developer-infra docs and product surfaces map well to
  Skillbox primitives: private GitHub Actions runners, SSH, Kubernetes ingress
  and egress, Tailscale Serve, tagged resources, ephemeral resources, and AI
  governance via Aperture.
- The June 2025 GitHub Action update explicitly lists deploys to internal
  servers, debugging private runners via SSH, private test database access, and
  private deployment monitoring as supported workflows. Those are close to the
  "agent runs tests and deploys through a private path" shape.
- The Tailscale SSH GA post frames SSH as a key user workflow and points to
  browser SSH sessions, remote port forwarding, SELinux, and session recording.
  The same thread on Lobsters shows the tradeoff: developers like the
  convenience, while security-minded users worry about concentrating service
  access behind Tailscale identity ([Tailscale SSH GA](https://tailscale.com/blog/tailscale-ssh-ga),
  [Lobsters Tailscale SSH](https://lobste.rs/s/y9ewni/introducing_tailscale_ssh)).
- Reddit self-hosted discussion around the 2026 pricing change is useful for
  Skillbox because it names the exact edge where Tailscale loses users:
  household/team seat limits, tagged-resource caps, and concern over future
  monetization. The same discussion mentions Headscale and NetBird as practical
  alternatives ([r/selfhosted pricing thread](https://www.reddit.com/r/selfhosted/comments/1sm3t5z/tailscale_improves_free_tier_3_free_users_is_now_6/)).

### Trend Direction

Trend direction: growing in developer infrastructure, unknown-to-growing in AI
agent tooling.

Confidence: medium.

Reasoning: official product investment, Homebrew volume, GitHub activity, and HN
mindshare all point up or at least healthy. The missing piece is direct evidence
that a large share of AI coding-agent builders already standardize on
Tailscale. For Skillbox, this means Tailscale is a credible default transport,
not a proven distribution channel by itself.

## Competing Private-Network Tools

### WireGuard DIY

- What it is and who uses it: WireGuard is the underlying VPN protocol and tool
  family for direct encrypted tunnels. DIY users configure keys, peers,
  endpoints, routes, and interface lifecycle using `wg`/`wg-quick`
  ([WireGuard quick start](https://www.wireguard.com/quickstart/)).
- Evidence of developer-infra adoption: WireGuard is the baseline many
  developers compare against. Reddit and Lobsters comments repeatedly frame
  Tailscale as "easier WireGuard" rather than a wholly separate category.
- Reasons developers choose it over Tailscale: no SaaS control plane, no pricing
  risk, no identity-provider dependency, smaller conceptual trust boundary.
- Reasons they avoid it: NAT traversal, onboarding, key rotation, multi-device
  access, ACLs, MagicDNS-like naming, and CI/ephemeral setup become manual.
- Confidence: high for "present as a comparison baseline"; low for direct
  AI-agent-runtime adoption counts.

### Cloudflare Tunnel

- What it is and who uses it: Cloudflare Tunnel uses `cloudflared` to create
  outbound-only connections from an origin to Cloudflare; it can connect web
  servers, SSH, remote desktop, and other protocols without a public routable IP
  ([Cloudflare Tunnel docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)).
- Evidence of developer-infra adoption: `cloudflare/cloudflared` has about 14.4k
  GitHub stars; HN threads around Cloudflare Tunnel include high-engagement
  discussions such as "Exposing a web service with Cloudflare Tunnel" and "SSH
  into private machines from anywhere using Cloudflare Tunnel"
  ([cloudflared](https://github.com/cloudflare/cloudflared),
  [HN 30257978](https://news.ycombinator.com/item?id=30257978),
  [HN 30283987](https://news.ycombinator.com/item?id=30283987)).
- Reasons developers choose it over Tailscale: HTTP/app exposure, browser/client
  access without requiring every user to join a tailnet, Cloudflare Access
  policies, familiar public-hostname workflows, and no inbound firewall holes.
- Reasons they avoid it: Cloudflare dependency, not a peer-to-peer private
  workstation mesh by default, and different ergonomics for arbitrary
  machine-to-machine dev access.
- Confidence: high for web/app exposure; medium for private coding-agent
  infrastructure.

### NetBird

- What it is and who uses it: NetBird is an open-source Zero Trust networking
  platform that creates WireGuard-based peer-to-peer overlay networks and can be
  cloud-hosted or self-hosted ([NetBird docs](https://docs.netbird.io/)).
- Evidence of developer-infra adoption: `netbirdio/netbird` has about 25.7k
  GitHub stars and 1.4k forks. NetBird docs emphasize near-zero configuration,
  direct encrypted tunnels, SSO, MFA, and access policies
  ([netbird](https://github.com/netbirdio/netbird)).
- Reasons developers choose it over Tailscale: full-stack open source posture,
  self-hosting with a UI/control plane, and a hedge against Tailscale pricing or
  control-plane dependency. Reddit comments in the 2026 Tailscale pricing thread
  explicitly recommend NetBird for this reason.
- Reasons they avoid it: smaller ecosystem, less ubiquitous developer mindshare,
  less obvious GitHub Actions/AI-agent integration story than Tailscale's
  official docs.
- Confidence: medium-high.

### ZeroTier

- What it is and who uses it: ZeroTier is a mature virtual-networking platform
  with a "secure, global networking platform" positioning
  ([ZeroTier docs](https://docs.zerotier.com/)).
- Evidence of developer-infra adoption: `zerotier/ZeroTierOne` has about 16.8k
  GitHub stars and long-term presence in P2P virtual networking
  ([ZeroTierOne](https://github.com/zerotier/ZeroTierOne)).
- Reasons developers choose it over Tailscale: longer-standing virtual LAN
  semantics, Layer-2-ish mental model, cross-device private networks, and an
  alternative vendor/control-plane risk profile.
- Reasons they avoid it: Tailscale has stronger current developer buzz,
  GitHub-action integration, MagicDNS/SSH/docs, and simpler onboarding for many
  modern dev workflows.
- Confidence: medium.

### Headscale

- What it is and who uses it: Headscale is a self-hosted implementation of the
  Tailscale control server, targeting self-hosters, hobbyists, personal use, and
  small open-source organizations
  ([headscale](https://github.com/juanfont/headscale)).
- Evidence of developer-infra adoption: Headscale has about 39.5k GitHub stars,
  2.2k forks, and multiple HN/Lobsters threads, including Lobsters
  "headscale: Open source, self-hosted implementation of the Tailscale control
  server" at score 46 / 6 comments
  ([headscale repo](https://github.com/juanfont/headscale),
  [Lobsters headscale](https://lobste.rs/s/f0wzzx/headscale_open_source_self_hosted)).
- Reasons developers choose it over Tailscale: keep Tailscale client ergonomics
  while owning the control plane; reduce SaaS/vendor risk; fit a self-hosted
  operator ideology.
- Reasons they avoid it: not full feature parity with Tailscale, more
  operational burden, public TLS/control-plane exposure still needs care, and
  some Tailscale features such as Serve/Funnel/App Connectors may be missing or
  different.
- Confidence: high for self-hosted-developer interest; medium for production
  agent-runtime adoption.

## Managed Agent Runtime Adoption

### E2B

- Evidence of adoption level: E2B's official site positions it as "AI Sandboxes"
  for enterprise-grade agents, lists case studies for Hugging Face, Manus, Groq,
  Lindy, and Perplexity, and claims 94 percent of Fortune 100 companies, 3.5M+
  monthly downloads, and 1B+ started sandboxes. The GitHub repo shows about
  12.4k stars, 927 forks, and 493 releases when checked
  ([E2B](https://e2b.dev/), [E2B docs](https://e2b.dev/docs),
  [E2B GitHub](https://github.com/e2b-dev/E2B)).
- Target segment: AI app builders, coding agents, computer-use agents, data
  analysis, CI/CD, and enterprise teams that need isolated execution for
  untrusted/generated code.
- Trend direction: growing.
- Confidence: high that E2B is growing in managed AI sandbox infrastructure;
  medium on independent-vs-enterprise split because the public site emphasizes
  enterprise metrics and named customer case studies.

### Daytona

- Evidence of adoption level: Daytona's official docs describe "full composable
  computers" for AI agents with dedicated kernel, filesystem, network stack,
  vCPU/RAM/disk, snapshots, SDKs, CLI, API, MCP server, and integrations for
  Claude, Cursor, and Windsurf. The GitHub repo shows about 72.5k stars and
  5.6k forks when checked
  ([Daytona sandboxes](https://www.daytona.io/docs/en/sandboxes/),
  [Daytona getting started](https://www.daytona.io/docs/getting-started),
  [Daytona GitHub](https://github.com/daytonaio/daytona)).
- Target segment: AI agent builders, evals, code interpreters, coding agents,
  computer use, data analysis, and teams needing secure generated-code
  execution.
- Trend direction: growing.
- Confidence: medium-high. The GitHub star count is a strong open-source signal
  but should not be treated as usage by itself.

### GitHub Copilot Cloud Agent / Codespaces-Adjacent Runtime

- Evidence of adoption level: GitHub announced Copilot coding agent on
  2025-05-19, calling it an asynchronous coding agent embedded in GitHub and VS
  Code. The same announcement says GitHub has 150M+ developers and 77k+
  organizations using Copilot. Current docs say Copilot cloud agent is available
  on all paid Copilot plans and in all GitHub repositories unless disabled
  ([GitHub announcement](https://github.com/newsroom/press-releases/coding-agent-for-github-copilot),
  [GitHub cloud agent access docs](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/access-management)).
- Target segment: both individual paid Copilot users and organizations. Business
  and Enterprise admins must enable policy controls, while Pro/Pro+/Max are
  enabled by default according to current docs.
- Trend direction: growing quickly due platform distribution.
- Confidence: high for growth and distribution; medium for actual active
  coding-agent usage because GitHub does not publish a clean active-agent usage
  count in the cited docs.

### Cursor Background Agents

- Evidence of adoption level: Cursor's official docs describe asynchronous
  remote agents that edit and run code in a remote environment, clone GitHub
  repositories, work on separate branches, support `.cursor/environment.json`,
  can use Dockerfiles, and run inside Cursor's AWS infrastructure in isolated
  VMs. The Background Agents API page says it is in beta and supports up to 256
  active agents per API key
  ([Cursor Background Agents](https://docs.cursor.com/background-agent),
  [Cursor Background Agents API](https://docs.cursor.com/background-agent/api/overview)).
- Target segment: Cursor users who want async tasks, PR branches, and agent
  execution without keeping the local machine busy.
- Trend direction: growing, but with visible UX maturity questions in community
  threads.
- Confidence: medium. Official capability is clear; adoption count not found.

### OpenAI Codex Cloud

- Evidence of adoption level: OpenAI introduced Codex on 2025-05-16 as a
  cloud-based software engineering agent where each task runs in its own cloud
  sandbox environment preloaded with the repository. OpenAI announced GA on
  2025-10-06 with Slack integration, SDK, admin tools, and cloud-task support
  ([Introducing Codex](https://openai.com/index/introducing-codex/),
  [Codex GA](https://openai.com/index/codex-now-generally-available/),
  [Codex plan help](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)).
- Target segment: ChatGPT Plus/Pro/Business/Enterprise/Edu users, engineering
  teams delegating tasks, and developers using local CLI/IDE plus cloud tasks.
- Trend direction: growing.
- Confidence: high for product momentum; medium for public adoption
  quantification because OpenAI does not publish active developer counts for
  Codex in the cited sources.

### Cross-Entity Synthesis

The managed tier is winning the "AI-agent runtime" narrative because the product
category is explicitly about running agents in sandboxes, VMs, cloud
environments, and PR workflows. E2B, Daytona, GitHub, Cursor, and OpenAI all
describe their runtimes in AI-agent terms. Tailscale describes private
connectivity and now AI governance, but the connective tissue between
"Tailscale users" and "AI coding-agent operators" remains mostly inferred.

Independent developers still have reasons to resist managed runtimes: local
control, cost predictability, repo privacy, persistent workspaces, toolchain
state, and SSH muscle memory. Enterprise teams have stronger reasons to adopt
managed runtimes: policy, audit logs, secrets controls, sandbox isolation,
parallel execution, PR workflow integration, and admin dashboards.

For Skillbox, that implies a wedge around serious independent operators and
small teams who want managed-like proof and orchestration without surrendering
the machine. It does not imply that the broad market will choose private boxes
over managed agent runtimes.

## Survey and Report Data

| Source | What it says | Private-box vs managed implication | Confidence |
|---|---|---|---|
| Stack Overflow Developer Survey 2025 | 84 percent of respondents use or plan to use AI tools, up from 76 percent in 2024; 51 percent of professional developers use AI tools daily; among software developers using AI agents at work, 84 percent use them for software development. Developers are resistant to high-responsibility systemic tasks such as deployment and monitoring, where 76 percent do not plan AI use ([Stack Overflow AI](https://survey.stackoverflow.co/2025/ai)). | Strong AI-coding-tool adoption, but not an infrastructure preference survey. It supports "agent usage is real," not "private boxes are preferred." | High |
| JetBrains AI tooling survey analysis 2026 | JetBrains says that by January 2026, 74 percent of developers worldwide had adopted specialized AI developer tools such as assistants, editors, and agents, not just chatbots ([JetBrains research blog](https://blog.jetbrains.com/research/2026/04/which-ai-coding-tools-do-developers-actually-use-at-work/)). | Strong AI tool adoption signal. No direct remote-runtime preference. | High |
| GitHub Octoverse 2025 | GitHub reports AI-related developer momentum, including major growth in AI-tagged projects and language shifts tied to AI workflows ([GitHub Octoverse 2025](https://github.blog/news-insights/octoverse/octoverse-a-new-developer-joins-github-every-second-as-ai-leads-typescript-to-1/)). | Supports "AI development is driving GitHub usage," but does not separate local/private/managed runtime preferences. | Medium |
| Coder/SlashData State of Development Environments 2025 | Coder says it surveyed over 550 software professionals at large organizations and found 78 percent of organizations planned to standardize environments within the year, with cloud-hosted solutions leading the charge ([Coder report page](https://coder.com/ebooks-and-reports/reports/state-of-development-environments-2025), [Coder summary blog](https://coder.com/blog/insights-and-key-findings-from-the-state-of-development-environments-2025-report)). | Useful for enterprise direction: standardization and cloud-hosted environments. Less useful for independent developers because the sample is large organizations and vendor-sponsored. | Medium |
| Go Developer Survey 2025 | The Go team discusses AI-powered development tools and developer concerns, including review overhead and mixed experience with agents ([Go survey](https://go.dev/blog/survey2025)). | Supports that serious developers are not uniformly enthusiastic and still care about review/control. It does not answer network/runtime preference. | Medium |

Survey conclusion: No high-quality public survey found that directly measures
"private durable dev box over Tailscale" vs "managed ephemeral/cloud AI-agent
runtime" preferences among independent AI-coding-agent operators. The nearest
proxies show high AI-tool adoption, enterprise environment standardization, and
developer concern over trust/review burden.

## Developer Community Sentiment

| Thread | Date and engagement | Signal | Skillbox implication |
|---|---:|---|---|
| [HN: Tailscale is pretty useful](https://news.ycombinator.com/item?id=43270835) | 2025-03-05; 804 points / 404 comments from HN Algolia search result | Tailscale has unusually strong developer mindshare and many practical use cases. | Tailscale default is culturally plausible for HN-style developers. |
| [HN: Tailscale SSH](https://news.ycombinator.com/item?id=31837115) and [Lobsters: Introducing Tailscale SSH](https://lobste.rs/s/y9ewni/introducing_tailscale_ssh) | HN 2022-06-22; 759 points / 303 comments. Lobsters 2022-06-22; score 44 / 36 comments. | Convenience is attractive, but concentrated identity/control-plane risk worries security-minded users. | Skillbox should keep SSH fallback and avoid requiring Tailscale SSH semantics for all collaborator access. |
| [Reddit r/selfhosted: Tailscale improves free tier](https://www.reddit.com/r/selfhosted/comments/1sm3t5z/tailscale_improves_free_tier_3_free_users_is_now_6/) | 2026-05-04 inferred from page "28d ago" on 2026-06-01; engagement count not exposed in static capture | Free tier expansion is liked, but users discuss NetBird, Headscale, WireGuard, tagged-resource caps, and future pricing risk. | Build transport boundaries so users can bring Headscale/NetBird/WireGuard without forking the whole model. |
| [Reddit r/Tailscale: small pricing update based on customer feedback](https://www.reddit.com/r/Tailscale/comments/1tcym7t/a_small_pricing_update_based_directly_on_customer/) | 2026-05-14 from search snippet; search snippet reported +132 votes; page shows tagged-resource pricing details | Tagged resources are the pressure point for sidecar/container-heavy developers. | Avoid a design that maps every agent/tool/container to a unique tagged Tailscale resource by default. |
| [HN: GitHub Copilot Coding Agent](https://news.ycombinator.com/item?id=44031432) | 2025-05-19; 564 points / 357 comments from HN Algolia search result | Managed coding-agent runtime announcements drive high engagement. | Managed cloud agents are not a theoretical future threat; they are already the visible default narrative. |
| [HN: Skill that lets Claude Code/Codex spin up VMs and GPUs](https://news.ycombinator.com/item?id=47006393) | 2026-02-13; 138 points / 36 comments from HN Algolia search result | There is demand for local/private-controlled compute escalation for agents. | Skillbox can win by making private compute delegation safer and more legible, not by merely saying "use my VPS." |
| [Lobsters: Your Container Is Not a Sandbox](https://lobste.rs/s/bznmaf/your_container_is_not_sandbox) | 2026-05-03; score 27 / 20 comments from Lobsters JSON | Developers are actively debating local sandbox threat models for Claude Code and similar agents. | Skillbox should frame private runtime safety around threat model, proof, and isolation, not generic "containers are safe" claims. |
| [Lobsters: The first AI agent worm is months away, if that](https://lobste.rs/s/osvwbe/first_ai_agent_worm_is_months_away_if) | 2026-03-06; score 30 / 35 comments from Lobsters JSON | Local authenticated agents create supply-chain and prompt-injection worries; some commenters point to cloud agents as safer. | Skillbox needs explicit guardrails for tokens, tool access, and network egress if it wants local/private to feel safer than cloud. |

## Research Questions Coverage

1. Current evidence for Tailscale adoption among developer-infra and
   AI-coding-agent users: answered. Developer-infra evidence is strong; direct
   AI-agent evidence is limited and mostly vendor/product-positioning plus
   workflow adjacency.
2. Competing private-networking approaches: answered for WireGuard DIY,
   Cloudflare Tunnel, NetBird, ZeroTier, and Headscale.
3. Tailscale free/personal tier and next tiers: answered using current pricing
   page and 2026 pricing update. Key change: Personal became more generous with
   6 users and unlimited user devices, while tagged resources are capped at 50
   with $1/month add-ons.
4. Managed runtime adoption for E2B, Daytona, GitHub Copilot cloud agent/Codespaces
   adjacent, Cursor hosted, and OpenAI Codex: answered with official docs,
   GitHub metrics, and vendor-published usage claims where available.
5. Public developer surveys: answered. Strong AI-tool adoption survey data was
   found; direct private-vs-managed AI-agent infrastructure survey data was not
   found.
6. Community sentiment on managed ephemeral vs private durable infra: answered
   with HN, Reddit, and Lobsters examples.

## Uncertainty Log

- Not found: a public, rigorous count of AI coding-agent projects that use
  Tailscale specifically as their network/access layer. Confidence: high that
  this was not found in the searched primary sources; needs human review if
  GitHub code search with authenticated API access is available.
- Inferred: Homebrew installs are a developer adoption proxy. They are not a
  clean measure of AI-agent operators. Confidence: medium.
- Inferred: Tailscale Aperture is evidence of Tailscale moving toward AI-agent
  governance, not evidence of broad Aperture adoption. Confidence: high.
- Not found: E2B independent-developer vs enterprise split. E2B publishes
  enterprise-heavy claims and open-source metrics, but not a clear cohort split.
  Confidence: medium.
- Not found: Daytona active usage or paid customer counts. GitHub stars are
  strong attention evidence, not usage. Confidence: high.
- Not found: Cursor Background Agents active usage counts. Official docs prove
  capability and hosted architecture only. Confidence: high.
- Not found: GitHub Copilot cloud agent active task count or solo-vs-enterprise
  split. GitHub publishes broad Copilot organization/developer platform counts,
  not active cloud-agent usage. Confidence: high.
- Not found: direct public survey question asking developers whether they prefer
  "private durable agent box" vs "managed ephemeral agent runtime." Confidence:
  high.
- Reddit engagement limitations: Reddit static pages exposed dates and content
  but not reliable score/comment counts for all threads in this environment.
  Where a search snippet exposed a vote count, it is labeled as search-snippet
  evidence rather than a full Reddit API extraction. Confidence: medium.

## Decision Implications for Skillbox

1. Keep Tailscale as the default transport and onboarding assumption.
2. Do not make Tailscale the only durable abstraction. Define internal concepts
   around private endpoints, identities, tags, SSH targets, and proof packets so
   Headscale/WireGuard/Cloudflare/NetBird can be added without rewriting the
   runtime model.
3. Avoid designing agent-per-container sidecars that consume many Tailscale
   tagged resources. Prefer shared nodes, scoped SSH users, runtime-level
   identity labels, or optional tagged-resource allocation only when needed.
4. Prioritize proof-first private runtime UX: current machine state, repo
   status, network reachability, guardrails, egress policy, secrets posture,
   and validation artifacts. This is the private-box equivalent of managed
   runtime dashboards.
5. Add a follow-up research bead only if product strategy depends on the size of
   the Tailscale-native AI-agent cohort. That follow-up should use authenticated
   GitHub code search, package/dependency graph mining, and manual review of
   top AI-agent repos.

## Source Index

- Tailscale pricing: <https://tailscale.com/pricing>
- Tailscale pricing v4: <https://tailscale.com/blog/pricing-v4>
- Tailscale GitHub Action announcement: <https://tailscale.com/blog/private-connections-for-github-actions>
- Tailscale SSH GA: <https://tailscale.com/blog/tailscale-ssh-ga>
- Tailscale Aperture / securing AI: <https://tailscale.com/use-cases/securing-ai>
- Tailscale Kubernetes docs: <https://tailscale.com/docs/kubernetes>
- Homebrew Tailscale formula API: <https://formulae.brew.sh/api/formula/tailscale.json>
- Homebrew Tailscale cask API: <https://formulae.brew.sh/api/cask/tailscale-app.json>
- GitHub Tailscale repo: <https://github.com/tailscale/tailscale>
- GitHub Tailscale Action: <https://github.com/tailscale/github-action>
- WireGuard quick start: <https://www.wireguard.com/quickstart/>
- Cloudflare Tunnel docs: <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/>
- NetBird docs: <https://docs.netbird.io/>
- ZeroTier docs: <https://docs.zerotier.com/>
- Headscale repo: <https://github.com/juanfont/headscale>
- E2B homepage: <https://e2b.dev/>
- E2B docs: <https://e2b.dev/docs>
- E2B repo: <https://github.com/e2b-dev/E2B>
- Daytona docs: <https://www.daytona.io/docs/en/sandboxes/>
- Daytona getting started: <https://www.daytona.io/docs/getting-started>
- Daytona repo: <https://github.com/daytonaio/daytona>
- GitHub Copilot coding agent announcement: <https://github.com/newsroom/press-releases/coding-agent-for-github-copilot>
- GitHub Copilot cloud agent docs: <https://docs.github.com/en/copilot/concepts/agents/cloud-agent/access-management>
- Cursor Background Agents docs: <https://docs.cursor.com/background-agent>
- Cursor Background Agents API docs: <https://docs.cursor.com/background-agent/api/overview>
- OpenAI Introducing Codex: <https://openai.com/index/introducing-codex/>
- OpenAI Codex GA: <https://openai.com/index/codex-now-generally-available/>
- Stack Overflow Developer Survey 2025 AI section: <https://survey.stackoverflow.co/2025/ai>
- JetBrains AI coding tools survey analysis: <https://blog.jetbrains.com/research/2026/04/which-ai-coding-tools-do-developers-actually-use-at-work/>
- GitHub Octoverse 2025: <https://github.blog/news-insights/octoverse/octoverse-a-new-developer-joins-github-every-second-as-ai-leads-typescript-to-1/>
- Coder State of Development Environments 2025: <https://coder.com/ebooks-and-reports/reports/state-of-development-environments-2025>
- Go Developer Survey 2025: <https://go.dev/blog/survey2025>
- HN Tailscale is pretty useful: <https://news.ycombinator.com/item?id=43270835>
- HN Tailscale SSH: <https://news.ycombinator.com/item?id=31837115>
- HN GitHub Copilot Coding Agent: <https://news.ycombinator.com/item?id=44031432>
- HN AI agent VM/GPU skill: <https://news.ycombinator.com/item?id=47006393>
- Lobsters Tailscale SSH: <https://lobste.rs/s/y9ewni/introducing_tailscale_ssh>
- Lobsters headscale: <https://lobste.rs/s/f0wzzx/headscale_open_source_self_hosted>
- Lobsters container sandbox thread: <https://lobste.rs/s/bznmaf/your_container_is_not_sandbox>
- Lobsters AI agent worm thread: <https://lobste.rs/s/osvwbe/first_ai_agent_worm_is_months_away_if>
- Reddit selfhosted Tailscale free tier thread: <https://www.reddit.com/r/selfhosted/comments/1sm3t5z/tailscale_improves_free_tier_3_free_users_is_now_6/>
- Reddit Tailscale tagged-resource pricing thread: <https://www.reddit.com/r/Tailscale/comments/1tcym7t/a_small_pricing_update_based_directly_on_customer/>
