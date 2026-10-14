# Hackathon rules: what Mandate must satisfy

Checked against each hackathon's official Devpost rules on 2026-10-11. Mandate is one codebase entered
three times, so each submission has to meet its own required-tech rule and show its own new work.

## Common to all three

| Rule | Status |
|---|---|
| Open-source licence visible in the repo's About section | MIT `LICENSE`. Set the licence in the GitHub About panel. |
| New or significantly updated during the submission window | Mandate started 2026-10-03, after all three windows opened (Aug 26, Aug 31, Oct 1). |
| Same project in several hackathons | None of the three rules forbids it. "Multiple Submissions" only limits duplicates within one hackathon. |
| Third-party SDKs/APIs used under their terms | PayPal sandbox (developer terms), Langfuse (MIT, self-hosted), Postgres/Redis (OSS). No problem. |
| Video < 3 min, public on YouTube, no third-party trademarks without permission | **Watch out:** the voice UI says "Alexa". That's fine in the Amazon video but not in the Nebius or PayPal videos. Use a neutral assistant name there. |
| Judges can run it | The default `docker compose up` runs offline (mock PayPal, rules parser), so no keys are needed. |

## Amazon: Build, Ship, Shape (Alexa+ track), deadline Oct 23, 12:00 PT

**No rule requires the whole architecture to be on Amazon technology.** The only hard requirement for Alexa+ is:

> A working Agent Skill or a self-hosted MCP server, implementing MCP spec version 2025-11-25 (or later) over
> Streamable HTTP, imported and actually called at runtime. Alternatively, a simulated Alexa+ experience.

| Requirement | Status |
|---|---|
| MCP spec **2025-11-25** over Streamable HTTP | **Was failing:** `mcp==1.9.4` negotiated `2025-03-26`, which would fail the pass/fail Stage One. Now `mcp==1.30.0`; `tests/redteam::test_mcp_server_speaks_spec_2025_11_25` asserts it. |
| Required tech used at runtime, not just named | `apps/mcp_server` imports and runs the MCP SDK. Tools carry titles and 2025-11-25 annotations. |
| AWS Builder mini-challenge (Bedrock etc., documented) | `LLM_PROVIDER=bedrock` uses the Converse API, defaulting to Amazon Nova 2 Lite. **Record the demo with Bedrock on.** Describe it in Product Feedback. |
| Product Feedback for every tool used | Required form fields. Cover MCP SDK, Bedrock, Alexa+ docs, AWS. |
| Friction log | Optional, worth up to 10% bonus. Real entries so far: Alexa+ MCP Toolkit is US-only and private preview; bare Bedrock model IDs fail on demand and need an inference profile; Alexa+ is in India but third-party add-ons aren't. |
| Open Source mini-challenge | Needs a contribution to a separate OSS repo, such as publishing `core/` as a standalone policy library. |

The tech score is judged on "how effectively it uses the required tech". Being Amazon-native helps, but nothing
non-Amazon is disallowed. PayPal for payments is fine. "Purchasing capabilities" is listed as a creative
Alexa+ use.

**Gap worth closing:** the web voice console calls the API directly. Routing the simulated Alexa+ through the MCP
server (a Bedrock tool-calling loop that discovers Mandate's MCP tools) would make the demo exercise the required
tech end to end.

## Nebius x NVIDIA (Personal AI track), deadline Oct 30, 10:00 PT

| Requirement | Status |
|---|---|
| Runtime call to Nebius Token Factory **or** deployed on Nebius AI Cloud | `LLM_PROVIDER=nebius` calls Token Factory (`api.tokenfactory.nebius.com/v1`, OpenAI-compatible). Needs a live check with your key: `scripts/live_check.py`. |
| Uses at least one NVIDIA open model | Default `nvidia/nemotron-3-super-120b-a12b`. The live check lists the Nemotron ids your key can see. |
| **URL to a working demo, hosted app or test build** | **Open.** Local compose isn't a hosted URL. Deploy on Nebius Serverless (suggested by the track) or similar. |
| README highlights Nemotron and Token Factory use | To do for the submission. |
| Not a "superficial rebrand" (Stage One) | Needs a real Nebius-specific feature, for example Nemotron Ultra for explain and what-if replay. |

## PayPal AI Hackathon, deadline Nov 12, 12:00 PT

| Requirement | Status |
|---|---|
| PayPal developer platform (sandbox) central, plus an AI tool | Orders v2 with `PayPal-Request-Id` idempotency, capture, refund, reconciler. **Only tested against the mock so far**; `tests/live/` exercises the real sandbox once you add keys. |
| Judges can run a working build | Setup instructions **plus** "any test account credentials, API keys, or sandbox login details necessary". Put sandbox keys in the Devpost testing instructions (not the public repo), or host it. |
| Sponsor prizes (Channel3, Render, ...) | Each has extra requirements in section 8 of the rules. Check them before targeting. |

## Observability (Langfuse) and the rules

Self-hosted Langfuse (MIT) is a permitted third-party integration in all three. It strengthens the Nebius
Personal AI story ("keeping your data under your control"), because traces of household speech never leave
your own infrastructure.
