# 🏗️ ১০০০ Role দিয়ে সম্পর্ণ Team Architecture (Fixed)

> প্রতিটি Team-এ **Workflow ও ধাপ অনুযায়ী Member** সাজানো হয়েছে।
>
> **যাচাইকৃত (verified):** প্রতিটি role নাম `roles.yaml` ও `roles.d/*.yaml`-এর আসল pack-এর
> সাথে মিলিয়ে দেখা হয়েছে, আর member সংখ্যা এখন গোনা — আর "আনুমানিক" নয়।
> `python3 scripts/build_teams.py --check` (CI-তেও চলে) এই ফাইলটা যাচাই করে।
>
> | পরিমাপ | সংখ্যা |
> |---|---|
> | Team | ৪৮ (Team 34-এর ১০টি sub-team নিয়ে মোট ৫৮টি block) |
> | Unique role | ১০০০ / ১০০০ — কোনো role বাদ পড়েনি |
> | Stage assignment | ১২৮৬ (একই role একাধিক team/stage-এ থাকলে বারবার গোনা) |
> | Pack mix (assignment) | ecc ৫৭০ · agency-agents ৩৫৫ · roles.yaml ১৭৭ · awesome-llm ১৫২ · security-practitioners ৪৯ · superpowers ২৩ · rea ৫ |
>
> একই role একাধিক team-এ থাকতে পারে (যেমন `forensics` Red ও Blue দুই team-এই, `ai-safety` দুই ধাপে),
> তাই assignment ১,০০০-এর বেশি — কিন্তু unique role ঠিক ১০০০।

---

## 🔴 TEAM 1: Red Team (Offensive Security)
**মোট Member: ৩৫** — যাচাইকৃত · ৮ ধাপ · ৩৫ assignment

```
Workflow: Scoping → Recon → Enumeration → Exploit → Post-Exploit → Report → Retest → Purple Team → Blue Team Handoff
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Scoping** | pentesting, security-audit, bug-bounty | roles.yaml |
| | security-penetration-tester, security-architect | agency-agents |
| **Recon** | osint, osint-investigation | roles.yaml, ecc |
| | dark-web-monitoring-analyst | security-practitioners |
| | web-scraping, data-scraper-agent | roles.yaml, ecc |
| **Enumeration** | network-security, attack-surface-management-specialist | roles.yaml, security-practitioners |
| | active-directory-attack-specialist | security-practitioners |
| | api-security-tester, database-security-pentester | security-practitioners |
| **Exploit** | web-application-pentester, mobile-app-pentester | security-practitioners |
| | wireless-security-auditor, iot-security-tester | security-practitioners |
| | cloud-pentester, ci-cd-pipeline-pentester | security-practitioners |
| | llm-ai-red-teamer | security-practitioners |
| **Post-Exploit** | reverse-engineering, malware-analysis, forensics | roles.yaml |
| | memory-forensics-analyst, network-forensics-analyst | security-practitioners |
| | mobile-forensics-examiner | security-practitioners |
| **Report** | security-bounty-hunter, security-review | ecc |
| | social-engineering-analyst | security-practitioners |
| **Retest** | ctf-player, ctf-mentor | roles.yaml, security-practitioners |
| | physical-security-assessor | security-practitioners |
| **Purple Team** | purple-team-coordinator | security-practitioners |

```
🔁 Handoff → Blue Team (Team 2)
```

---

## 🔵 TEAM 2: Blue Team (Defensive Security)
**মোট Member: ২৮** — যাচাইকৃত · ৭ ধাপ · ২৮ assignment

```
Workflow: Threat Intel → Detection → Monitoring → Incident Response → Forensics → Hardening → Compliance
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Threat Intel** | security-threat-intelligence-analyst | agency-agents |
| | security-threat-detection-engineer | agency-agents |
| | dark-web-monitoring-analyst | security-practitioners |
| **Detection** | soc-analyst, siem-engineer | security-practitioners |
| | blue-team | roles.yaml |
| | security-scan, security-review | ecc |
| **Monitoring** | observability | roles.yaml |
| | soar-engineer | security-practitioners |
| | deception-technology-specialist | security-practitioners |
| **Incident Response** | engineering-incident-response-commander | agency-agents |
| | security-incident-responder | agency-agents |
| | safety-guard | ecc |
| **Forensics** | forensics, reverse-engineering | roles.yaml |
| | memory-forensics-analyst, network-forensics-analyst | security-practitioners |
| | mobile-forensics-examiner | security-practitioners |
| **Hardening** | security-hardening-advisor | security-practitioners |
| | zero-trust-architect | security-practitioners |
| | container-kubernetes-security-specialist | security-practitioners |
| | ics-scada-security-specialist | security-practitioners |
| **Compliance** | compliance, security-compliance-auditor | roles.yaml, agency-agents |
| | iam, engineering-identity-access-engineer | roles.yaml, agency-agents |
| | saas-tenant-isolation-assessor | security-practitioners |

```
🔁 Handoff → DevSecOps Team (Team 3) — Continuous Integration
```

---

## 🟣 TEAM 3: DevSecOps Pipeline Team
**মোট Member: ২১** — যাচাইকৃত · ৭ ধাপ · ২১ assignment

```
Workflow: Code Commit → SAST/DAST → Dependency Scan → Secret Scan → Container Scan → Deploy Gate → Monitor
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Code Commit** | devsecops, engineering-code-reviewer | roles.yaml, agency-agents |
| | conventional-commit-review, git-workflow | ecc |
| **SAST/DAST** | security-ai-generated-code-auditor | agency-agents |
| | application-security-engineer | agency-agents |
| | code-reviewer-ecc, security-reviewer | ecc |
| **Dependency Scan** | dependency-doctor, mcp-dependency-review | awesome-llm, ecc |
| | software-supply-chain-security-researcher | security-practitioners |
| **Secret Scan** | security-secrets-credential-engineer | agency-agents |
| | cryptography | roles.yaml |
| **Container Scan** | container-kubernetes-security-specialist | security-practitioners |
| | docker-patterns, kubernetes-patterns | ecc |
| **Deploy Gate** | delivery-gate, gateguard | ecc |
| | release-engineering | roles.yaml |
| **Monitor** | security-senior-secops | agency-agents |
| | canary-watch | ecc |

```
🔁 Handoff → Platform Engineering Team (Team 6)
```

---

## 🟡 TEAM 4: Vulnerability Research Team
**মোট Member: ১৮** — যাচাইকৃত · ৭ ধাপ · ১৮ assignment

```
Workflow: Target Selection → Fuzzing → Root Cause → PoC → Disclosure → Patch Verify
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Target Selection** | bug-bounty, attack-surface-management-specialist | roles.yaml, security-practitioners |
| **Fuzzing** | coverage-guided-fuzzing-expert | security-practitioners |
| | firmware-security-researcher | security-practitioners |
| **Root Cause** | vulnerability-root-cause-researcher | security-practitioners |
| | browser-security-researcher | security-practitioners |
| | network-protocol-security-researcher | security-practitioners |
| **PoC Development** | reverse-engineering, malware-analysis | roles.yaml |
| | hardware-root-of-trust-security-assessor | security-practitioners |
| **Disclosure** | security-review, security-bounty-hunter | ecc |
| **Protocol Audit** | oauth-oidc-security-specialist | security-practitioners |
| | saml-federation-security-assessor | security-practitioners |
| **Blockchain Audit** | security-blockchain-security-auditor | agency-agents |
| | defi-amm-security, evm-token-decimals | ecc |
| | llm-trading-agent-security | ecc |

```
🔁 Handoff → Red Team (retest) অথবা Blue Team (patch verify)
```

---

## 🟢 TEAM 5: Full-Stack Product Engineering Team
**মোট Member: ৭৭** — যাচাইকৃত · ৯ ধাপ · ৭৭ assignment

```
Workflow: Requirements → Architecture → Frontend → Backend → Database → API → Testing → Deploy → Monitor
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Requirements** | product-management, business-analysis | roles.yaml |
| | product-manager, product-feedback-synthesizer | agency-agents |
| | spec-miner, planner | ecc |
| **Architecture** | architecture, engineering-software-architect | roles.yaml, agency-agents |
| | architect, code-architect, hexagonal-architecture | ecc |
| | architecture-decision-records | ecc |
| | ai-system-architect-r1 | awesome-llm |
| **Frontend** | frontend, react, css, web-performance | roles.yaml |
| | engineering-frontend-developer | agency-agents |
| | react-patterns, react-performance, vue-patterns | ecc |
| | angular-developer, nextjs-turbopack, nuxt4-patterns | ecc |
| | tailwind, vite-patterns, frontend-patterns | ecc |
| | ui-to-vue, frontend-design-direction | ecc |
| | engineering-senior-developer | agency-agents |
| **Backend** | backend, engineering-backend-architect | roles.yaml, agency-agents |
| | backend-architect-with-memory | agency-agents |
| | backend-patterns, nestjs-patterns | ecc |
| | django-patterns, fastapi-patterns, rails-patterns | ecc |
| | springboot-patterns, laravel-patterns, quarkus-patterns | ecc |
| | fullstack | roles.yaml |
| **Database** | database, nosql | roles.yaml |
| | engineering-database-optimizer | agency-agents |
| | engineering-database-reliability-engineer | agency-agents |
| | postgres-patterns, mysql-patterns, redis-patterns | ecc |
| | clickhouse-io, prisma-patterns, database-migrations | ecc |
| | database-reviewer | ecc |
| **API** | api-design | roles.yaml |
| | engineering-api-platform-engineer | agency-agents |
| | api-design-ecc, api-connector-builder | ecc |
| **Testing** | qa, test-automation, performance-testing | roles.yaml |
| | testing-test-automation-engineer | agency-agents |
| | tdd-workflow, e2e-testing, browser-qa | ecc |
| | e2e-runner, test-collaboration, module-regression | ecc |
| **Deploy** | devops, cloud-infra, release-engineering | roles.yaml |
| | engineering-devops-automator | agency-agents |
| | deployment-patterns, docker-patterns | ecc |
| **Monitor** | observability | roles.yaml |
| | sre-site-reliability-engineer | agency-agents |
| | canary-watch, production-audit | ecc |

```
🔁 Handoff → QA Team (Team 7) → DevSecOps (Team 3) → Release
```

---

## 🔶 TEAM 6: Platform & Infrastructure Team
**মোট Member: ৪০** — যাচাইকৃত · ৮ ধাপ · ৪০ assignment

```
Workflow: Infra Design → Provisioning → CI/CD → Networking → Monitoring → Cost Optimization → Incident
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Infra Design** | cloud-infra, devops | roles.yaml |
| | engineering-platform-engineer | agency-agents |
| | homelab-architect | ecc |
| **Provisioning** | security-cloud-security-architect | agency-agents |
| | docker-patterns, kubernetes-patterns | ecc |
| | uncloud, flox-environments | ecc |
| **CI/CD** | release-engineering | roles.yaml |
| | engineering-mobile-release-engineer | agency-agents |
| | github-ops, orch-pipeline | ecc |
| | opensource-pipeline | ecc |
| **Networking** | network-security | roles.yaml |
| | engineering-network-engineer | agency-agents |
| | network-architect, network-troubleshooter | ecc |
| | network-config-validation, network-interface-health | ecc |
| | network-bgp-diagnostics, netmiko-ssh-automation | ecc |
| | cisco-ios-patterns | ecc |
| | network-config-reviewer | ecc |
| **Homelab/Edge** | homelab-network-readiness, homelab-network-setup | ecc |
| | homelab-pihole-dns, homelab-vlan-segmentation | ecc |
| | homelab-wireguard-vpn | ecc |
| **Cost Optimization** | engineering-finops-engineer | agency-agents |
| | cost-aware-llm-pipeline, cost-tracking | ecc |
| | ecc-tools-cost-audit, token-budget-advisor | ecc |
| **Monitoring** | observability | roles.yaml |
| | sre-site-reliability-engineer | agency-agents |
| | production-audit, canary-watch | ecc |
| **Incident** | engineering-incident-response-commander | agency-agents |
| | chaos-engineering | roles.yaml |

```
🔁 Handoff → DevSecOps (Team 3) — Security validation
```
*(ঠিক করা হয়েছে: আগে "engineering-cloud-security-architect (security-practitioners)" ভুল করে লেখা ছিল — সঠিক role হলো agency-agents ফাইলের security-cloud-security-architect।)*

---

## 🧪 TEAM 7: QA & Testing Team
**মোট Member: ৩৮** — যাচাইকৃত · ৭ ধাপ · ৩৮ assignment

```
Workflow: Test Planning → Unit/Integration → E2E → Performance → Accessibility → Security Test → Report
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Test Planning** | qa, test-automation | roles.yaml |
| | testing-workflow-optimizer | agency-agents |
| | tdd-guide, tdd-workflow | ecc |
| **Unit/Integration** | testing-test-automation-engineer | agency-agents |
| | react-testing, python-testing, rust-testing | ecc |
| | golang-testing, kotlin-testing, cpp-testing | ecc |
| | csharp-testing, fsharp-testing, perl-testing | ecc |
| | swift-protocol-di-testing | ecc |
| **E2E** | e2e-testing, e2e-runner, browser-qa | ecc |
| | click-path-audit, windows-desktop-e2e | ecc |
| | testing-evidence-collector | agency-agents |
| **Performance** | performance-testing | roles.yaml |
| | testing-performance-benchmarker | agency-agents |
| | benchmark, benchmark-methodology | ecc |
| | benchmark-optimization-loop, data-throughput-accelerator | ecc |
| **Accessibility** | accessibility | roles.yaml |
| | testing-accessibility-auditor | agency-agents |
| | section-508-accessibility-specialist | agency-agents |
| | accessibility-ecc, frontend-a11y | ecc |
| | a11y-architect | ecc |
| **API Testing** | testing-api-tester | agency-agents |
| **Reality Check** | testing-reality-checker, testing-tool-evaluator | agency-agents |
| | testing-test-results-analyzer | agency-agents |

```
🔁 Handoff → Product Team (sign-off) → Release Engineering
```

---

## 🤖 TEAM 8: AI/ML Engineering Team
**মোট Member: ৭২** — যাচাইকৃত · ৭ ধাপ · ৭৪ assignment

```
Workflow: Research → Data Prep → Model Training → Evaluation → RAG/Agent Build → Deploy → Monitor
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Research** | research, ai-safety | roles.yaml |
| | ai-deep-research-agent, ai-domain-deep-research-agent | awesome-llm |
| | openai-research-agent, multi-agent-researcher | awesome-llm |
| | deep-research, exa-search | ecc |
| | scientific-thinking-literature-review | ecc |
| | scientific-thinking-scholar-evaluation | ecc |
| | ai-reasoning-agent, ai-knowledge-explorer | awesome-llm |
| **Data Prep** | data-engineering, data-science | roles.yaml |
| | engineering-data-engineer | agency-agents |
| | ai-data-analysis-agent, ai-data-visualisation-agent | awesome-llm |
| | engineering-data-visualization-engineer | agency-agents |
| | bengali-nlp | ecc |
| **Model Training** | ml-ops, ai-engineering | roles.yaml |
| | engineering-llm-post-training-engineer | agency-agents |
| | engineering-ai-engineer | agency-agents |
| | mle-workflow, mle-reviewer | ecc |
| | ito-training, ito-compute, ito-inference | ecc |
| | pytorch-patterns, pytorch-build-resolver | ecc |
| | foundation-models-on-device | ecc |
| **Evaluation** | ai-safety | roles.yaml |
| | agent-eval, agent-self-evaluation | ecc |
| | eval-harness, gan-evaluator | ecc |
| | specialized-model-qa | agency-agents |
| | healthcare-eval-harness | ecc |
| | ai-regression-testing | ecc |
| **RAG Pipeline** | rag-pipeline-reviewer | ecc |
| | agentic-rag-gpt5, agentic-rag-with-reasoning | awesome-llm |
| | agentic-rag-embedding-gemma, autonomous-rag | awesome-llm |
| | corrective-rag, hybrid-search-rag | awesome-llm |
| | knowledge-graph-rag-citations, vision-rag | awesome-llm |
| | contextualai-rag-agent, rag-as-a-service | awesome-llm |
| | local-rag-agent, deepseek-local-rag-agent | awesome-llm |
| | rag-chain, rag-database-routing | awesome-llm |
| | rag-failure-diagnostics-clinic | awesome-llm |
| | agentic-rag-math-agent, agentic-typed-rag-pydanticai | awesome-llm |
| | gemini-agentic-rag, llama3-1-local-rag, qwen-local-rag | awesome-llm |
| | local-hybrid-search-rag, rag-agent-cohere | awesome-llm |
| **Agent Build** | engineering-multi-agent-systems-architect | agency-agents |
| | agentic-engineering, agentic-os | ecc |
| | agent-architecture-audit | ecc |
| | multi-agent-trust-layer, trust-gated-agent-team | awesome-llm |
| | ai-agent-governance, ai-self-evolving-agent | awesome-llm |
| **Deploy/Monitor** | ml-ops | roles.yaml |
| | agent-runtime-gateway-smoke-test | ecc |
| | recsys-pipeline-architect | ecc |
| | ml-adoption-playbook | ecc |

```
🔁 Handoff → Platform Team (deploy) → DevSecOps (security scan)
```

---

## 💬 TEAM 9: NLP & Computer Vision Team
**মোট Member: ১৭** — যাচাইকৃত · ৫ ধাপ · ১৭ assignment

```
Workflow: Data Collection → Annotation → Model Train → Fine-tune → Evaluate → API Serve
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Data Collection** | web-scraping | roles.yaml |
| | web-scraping-ai-agent | awesome-llm |
| | scientific-db-pubmed-database, scientific-db-uspto-database | ecc |
| **NLP** | nlp | roles.yaml |
| | bengali-nlp | ecc |
| | language-translator | agency-agents |
| | translation | roles.yaml |
| **Computer Vision** | computer-vision | roles.yaml |
| | ai-medical-imaging-agent | awesome-llm |
| **Multimodal** | multimodal-ai-agent | awesome-llm |
| | multimodal-coding-agent-team | awesome-llm |
| **Voice** | ai-audio-tour-agent, customer-support-voice-agent | awesome-llm |
| | voice-rag-openaisdk | awesome-llm |
| | ai-speech-trainer-agent | awesome-llm |
| | engineering-voice-ai-integration-engineer | agency-agents |

```
🔁 Handoff → AI/ML Team (integration) → Product Team
```

---

## 🎮 TEAM 10: Game Development Team
**মোট Member: ৩৫** — যাচাইকৃত · ৬ ধাপ · ৩৫ assignment

```
Workflow: Concept → Design → Art/Audio → Programming → Networking → Testing → Polish → Launch
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Concept** | game-design | roles.yaml |
| | game-designer, narrative-designer | agency-agents |
| | economy-designer, level-designer | agency-agents |
| | ai-game-design-agent-team | awesome-llm |
| **Art/Audio** | illustration, motion-graphics | roles.yaml |
| | technical-artist | agency-agents |
| | unreal-technical-artist, unity-shader-graph-artist | agency-agents |
| | godot-shader-developer | agency-agents |
| | game-audio-engineer | agency-agents |
| | blender-add-on-engineer | agency-agents |
| | design-image-prompt-engineer | agency-agents |
| **Programming** | game-dev | roles.yaml |
| | unity-architect, unity-editor-tool-developer | agency-agents |
| | unreal-systems-engineer, unreal-world-builder | agency-agents |
| | godot-gameplay-scripter | agency-agents |
| | roblox-systems-scripter, roblox-experience-designer | agency-agents |
| | roblox-avatar-creator | agency-agents |
| **Networking** | unity-multiplayer-engineer | agency-agents |
| | unreal-multiplayer-architect | agency-agents |
| | godot-multiplayer-engineer | agency-agents |
| **3D/XR** | visionos-spatial-engineer | agency-agents |
| | xr-immersive-developer, xr-interface-architect | agency-agents |
| | xr-cockpit-interaction-specialist | agency-agents |
| | macos-spatial-metal-engineer | agency-agents |
| **Testing** | ai-chess-agent, ai-tic-tac-toe-agent | awesome-llm |
| | ai-3dpygame-r1 | awesome-llm |

```
🔁 Handoff → QA Team → Release → Marketing Team
```

---

## 📱 TEAM 11: Mobile & Cross-Platform Team
**মোট Member: ২৮** — যাচাইকৃত · ৭ ধাপ · ২৮ assignment

```
Workflow: Design → Native Dev → Cross-Platform → Build/Resolve → Test → Release → Monitor
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Design** | design, design-ui-designer, design-ux-architect | roles.yaml, agency-agents |
| **Android** | android | roles.yaml |
| | android-clean-architecture | ecc |
| | kotlin-patterns, kotlin-coroutines-flows | ecc |
| | kotlin-build-resolver, kotlin-reviewer | ecc |
| **iOS** | ios | roles.yaml |
| | swift-build-resolver, swift-reviewer | ecc |
| | swift-concurrency-6-2, swift-actor-persistence | ecc |
| | swiftui-patterns | ecc |
| **Cross-Platform** | cross-platform | roles.yaml |
| | engineering-mobile-app-builder | agency-agents |
| | flutter-reviewer, flutter-dart-code-review | ecc |
| | dart-build-resolver, dart-flutter-patterns | ecc |
| | compose-multiplatform-patterns | ecc |
| | react-native-patterns | ecc |
| | mobile | roles.yaml |
| **Build/Resolve** | harmonyos-app-resolver | ecc |
| **Release** | engineering-mobile-release-engineer | agency-agents |
| **Testing** | mobile-app-pentester | security-practitioners |
| | mobile-forensics-examiner | security-practitioners |

```
🔁 Handoff → QA Team → App Store (marketing-app-store-optimizer)
```

---

## 🖥️ TEAM 12: Desktop & Embedded Team
**মোট Member: ১৮** — যাচাইকৃত · ৫ ধাপ · ১৮ assignment

```
Workflow: Requirements → Architecture → Development → Firmware → Testing → Deploy
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Requirements** | desktop, embedded | roles.yaml |
| **Architecture** | engineering-desktop-app-engineer | agency-agents |
| | engineering-embedded-firmware-engineer | agency-agents |
| **Development** | rust (for systems), golang | roles.yaml |
| | rust-patterns, rust-testing, rust-build-resolver | ecc |
| | engineering-rust-refactoring-specialist | agency-agents |
| | engineering-webassembly-engineer | agency-agents |
| | engineering-iot-fleet-engineer | agency-agents |
| **Terminal** | terminal-integration-specialist | agency-agents |
| | terminal-ops, terminal-opener | ecc |
| **Testing** | windows-desktop-e2e | ecc |
| | windows-use-autonomous-agent | awesome-llm |
| | firmware-security-researcher | security-practitioners |

```
🔁 Handoff → QA Team → Platform Team (deploy)
```

---

## ⛓️ TEAM 13: Blockchain & DeFi Team
**মোট Member: ১৭** — যাচাইকৃত · ৫ ধাপ · ১৮ assignment

```
Workflow: Research → Smart Contract Dev → Audit → Deploy → Monitor → Incident Response
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Research** | blockchain, defi-analyst | roles.yaml |
| | quant-research | roles.yaml |
| | prediction-market-oracle-research | ecc |
| | prediction-market-risk-review | ecc |
| **Smart Contract Dev** | engineering-solidity-smart-contract-engineer | agency-agents |
| | nodejs-keccak256 | ecc |
| **Audit** | security-blockchain-security-auditor | agency-agents |
| | defi-amm-security | ecc |
| | evm-token-decimals | ecc |
| **Trading** | trading, risk-management | roles.yaml |
| | llm-trading-agent-security | ecc |
| | ai-investment-agent | awesome-llm |
| **DeFi** | defi-analyst | roles.yaml |
| | ai-personal-finance-agent | awesome-llm |
| | ai-x402-paying-agent, agent-payment-x402 | awesome-llm, ecc |

```
🔁 Handoff → Security Team (audit) → Blue Team (monitor)
```

---

## 📊 TEAM 14: Data Engineering & Analytics Team
**মোট Member: ২৮** — যাচাইকৃত · ৭ ধাপ · ২৮ assignment

```
Workflow: Ingest → Transform → Store → Analyze → Visualize → Report → Predict
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Ingest** | data-engineering | roles.yaml |
| | engineering-data-engineer | agency-agents |
| | data-scraper-agent, enrichment-agent | ecc |
| | web-scraping-ai-agent | awesome-llm |
| **Transform** | engineering-ai-data-remediation-engineer | agency-agents |
| | data-consolidation-agent | agency-agents |
| **Store** | database, nosql | roles.yaml |
| | engineering-database-optimizer | agency-agents |
| | postgres-patterns, mysql-patterns, clickhouse-io | ecc |
| **Analyze** | analytics, data-science | roles.yaml |
| | ai-data-analysis-agent | awesome-llm |
| | support-analytics-reporter | agency-agents |
| | finance-financial-analyst | agency-agents |
| **Visualize** | engineering-data-visualization-engineer | agency-agents |
| | ai-data-visualisation-agent | awesome-llm |
| | ai-dashboard-canvas-agent | awesome-llm |
| | dashboard-builder | ecc |
| | charts (remotion) | ecc |
| **Report** | support-executive-summary-generator | agency-agents |
| | report-distribution-agent | agency-agents |
| **Predict** | quant-research | roles.yaml |
| | ito-baskets, ito-inference | ecc |

```
🔁 Handoff → Product Team (insights) → Business Strategy Team
```

---

## 📣 TEAM 15: Marketing & Growth Team
**মোট Member: ৭৮** — যাচাইকৃত · ১৪ ধাপ · ৭৮ assignment

```
Workflow: Research → Strategy → Content Create → SEO/AEO → Distribute → Paid Media → Analyze → Optimize
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Research** | product-trend-researcher | agency-agents |
| | market-research, competitive-platform-analysis | ecc |
| | competitive-report-structure | ecc |
| | brand-discovery | ecc |
| | ai-competitor-intelligence-agent-team | awesome-llm |
| **Strategy** | marketing, content-strategy | roles.yaml |
| | marketing-growth-hacker | agency-agents |
| | marketing-agent, marketing-campaign | ecc |
| | business-strategist | agency-agents |
| | product-behavioral-nudge-engine | agency-agents |
| **Content Creation** | copywriting, content-writing | roles.yaml |
| | marketing-content-creator | agency-agents |
| | marketing-book-co-author | agency-agents |
| | content-engine, article-writing | ecc |
| | brand-voice | ecc |
| | ai-blog-to-podcast-agent | awesome-llm |
| **SEO/AEO** | seo | roles.yaml |
| | marketing-seo-specialist | agency-agents |
| | marketing-aeo-foundations, marketing-agentic-search-optimizer | agency-agents |
| | marketing-ai-citation-strategist | agency-agents |
| | marketing-baidu-seo-specialist | agency-agents |
| | seo-ecc, seo-specialist-ecc | ecc |
| | ai-seo-audit-team | awesome-llm |
| **Social Media** | social-media | roles.yaml |
| | marketing-social-media-strategist | agency-agents |
| | marketing-tiktok-strategist, marketing-douyin-strategist | agency-agents |
| | marketing-instagram-curator | agency-agents |
| | marketing-linkedin-content-creator | agency-agents |
| | marketing-twitter-engager, marketing-x-twitter-intelligence-analyst | agency-agents |
| | marketing-reddit-community-builder | agency-agents |
| | marketing-kuaishou-strategist | agency-agents |
| | marketing-bilibili-content-strategist | agency-agents |
| | marketing-weibo-strategist | agency-agents |
| | marketing-wechat-official-account | agency-agents |
| | marketing-xiaohongshu-specialist | agency-agents |
| | marketing-zhihu-strategist | agency-agents |
| | social-publisher, crosspost | ecc |
| **Email** | email-marketing | roles.yaml |
| | marketing-email-strategist | agency-agents |
| | ai-email-gtm-outreach-agent, ai-email-gtm-reachout-agent | awesome-llm |
| | email-ops | ecc |
| **Podcast/Video** | podcast-production, video-script | roles.yaml |
| | marketing-podcast-strategist, marketing-global-podcast-strategist | agency-agents |
| | marketing-video-optimization-specialist | agency-agents |
| | marketing-short-video-editing-coach | agency-agents |
| | marketing-livestream-commerce-coach | agency-agents |
| | marketing-carousel-growth-engine | agency-agents |
| **Paid Media** | paid-media-ppc-strategist | agency-agents |
| | paid-media-paid-social-strategist | agency-agents |
| | paid-media-programmatic-buyer | agency-agents |
| | paid-media-creative-strategist | agency-agents |
| | paid-media-auditor | agency-agents |
| | paid-media-search-query-analyst | agency-agents |
| | tracking-measurement-specialist | agency-agents |
| **PR** | marketing-pr-communications-manager | agency-agents |
| **China Market** | cross-border-e-commerce-specialist | agency-agents |
| | marketing-china-market-localization-strategist | agency-agents |
| | marketing-china-ecommerce-operator | agency-agents |
| | marketing-private-domain-operator | agency-agents |
| **App Store** | marketing-app-store-optimizer | agency-agents |
| **Multi-Platform** | marketing-multi-platform-publisher | agency-agents |
| **Developer Marketing** | marketing-developer-community-builder | agency-agents |
| | specialized-developer-advocate | agency-agents |
| **Analytics** | growth-log, signal-scorer | ecc |

```
🔁 Handoff → Sales Team (leads) → Product Team (feedback)
```

---

## 💰 TEAM 16: Sales & Revenue Team
**মোট Member: ২৪** — যাচাইকৃত · ৭ ধাপ · ২৪ assignment

```
Workflow: Lead Gen → Research → Discovery → Proposal → Negotiation → Close → Handoff to CS
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Lead Gen** | sales-offer-lead-gen-strategist | agency-agents |
| | sales-outbound-strategist | agency-agents |
| | sales-outreach, sales-data-extraction-agent | agency-agents |
| | lead-intelligence | ecc |
| | ai-sales-intelligence-agent-team | awesome-llm |
| **Research** | sales-account-strategist | agency-agents |
| | sales-pipeline-analyst | agency-agents |
| | connections-optimizer, mutual-mapper | ecc |
| | social-graph-ranker | ecc |
| **Discovery** | sales-discovery-coach | agency-agents |
| | sales-coach | agency-agents |
| **Proposal** | sales-proposal-strategist | agency-agents |
| | sales-engineer | agency-agents |
| | outreach-drafter | ecc |
| **Deal Strategy** | sales-deal-strategist | agency-agents |
| | specialized-pricing-analyst | agency-agents |
| **Close** | sales | roles.yaml |
| **Customer Success** | customer-success-manager | agency-agents |
| | customer-service, support-support-responder | agency-agents |
| | ai-customer-support-agent | awesome-llm |
| | retail-customer-returns | agency-agents |

```
🔁 Handoff → Customer Success → Support Team → Product (feedback loop)
```

---

## 💼 TEAM 17: Finance & Accounting Team
**মোট Member: ২৫** — যাচাইকৃত · ৮ ধাপ · ২৫ assignment

```
Workflow: Bookkeeping → Analysis → Tax Strategy → Investment → Risk → Billing → Reporting
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Bookkeeping** | finance-bookkeeper-controller | agency-agents |
| | accounts-payable-agent | agency-agents |
| | support-finance-tracker | agency-agents |
| **Analysis** | finance, finance-financial-analyst | roles.yaml, agency-agents |
| | finance-fpa-analyst | agency-agents |
| | ai-finance-agent-team, xai-finance-agent | awesome-llm |
| | ai-financial-coach-agent | awesome-llm |
| **Tax** | finance-tax-strategist | agency-agents |
| **Investment** | finance-investment-researcher | agency-agents |
| | ai-investment-agent | awesome-llm |
| | ai-vc-due-diligence-agent-team | awesome-llm |
| | investor-materials, investor-outreach | ecc |
| **Risk** | risk-management | roles.yaml |
| | ai-fraud-investigation-agent | awesome-llm |
| **Billing** | engineering-payments-billing-engineer | agency-agents |
| | customer-billing-ops, finance-billing-ops | ecc |
| | loan-officer-assistant | agency-agents |
| **Insurance** | ai-life-insurance-advisor-agent | awesome-llm |
| | insurance-claim-live-agent-team | awesome-llm |
| **Reporting** | chief-financial-officer | agency-agents |
| | earnings-call-analyst-agent | awesome-llm |

```
🔁 Handoff → Legal Team (compliance) → Business Strategy
```

---

## ⚖️ TEAM 18: Legal & Compliance Team
**মোট Member: ১৮** — যাচাইকৃত · ৮ ধাপ · ১৮ assignment

```
Workflow: Intake → Review → Draft → Compliance Check → Audit → Privacy → ESG
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Intake** | legal-client-intake | agency-agents |
| **Review** | legal-document-review | agency-agents |
| | ai-legal-agent-team, local-ai-legal-agent-team | awesome-llm |
| | support-legal-compliance-checker | agency-agents |
| **Draft** | legal-draft | roles.yaml |
| | master-agreement-generator | ecc |
| | visa-doc-translate | ecc |
| **Billing** | legal-billing-time-tracking | agency-agents |
| **Compliance** | compliance | roles.yaml |
| | security-compliance-auditor | agency-agents |
| | specialized-fedramp-rmf-compliance | agency-agents |
| | hipaa-compliance, healthcare-phi-compliance | ecc |
| **Privacy** | data-privacy-officer | agency-agents |
| | engineering-privacy-engineer | agency-agents |
| **ESG** | esg-sustainability-officer | agency-agents |
| **Marketing Compliance** | healthcare-marketing-compliance-specialist | agency-agents |

```
🔁 Handoff → Finance Team → HR Team → Business Strategy
```

---

## 👥 TEAM 19: HR & People Operations Team
**মোট Member: ১৬** — যাচাইকৃত · ৬ ধাপ · ১৬ assignment

```
Workflow: Recruitment → Screening → Onboarding → Training → Career Dev → Retention
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Recruitment** | hr | roles.yaml |
| | recruitment-specialist | agency-agents |
| | ai-recruitment-agent-team | awesome-llm |
| | engineering-ats-validator-architect | agency-agents |
| **Screening** | resume-tailor | agency-agents |
| **Onboarding** | hr-onboarding | agency-agents |
| | codebase-onboarding, engineering-codebase-onboarding-engineer | ecc, agency-agents |
| **Training** | corporate-training-designer | agency-agents |
| | tutorial-writing | roles.yaml |
| | ai-teaching-agent-team | awesome-llm |
| **Career Development** | personal-growth-mentor | agency-agents |
| | adk-career-coach-agent-memory | awesome-llm |
| | study-abroad-advisor | agency-agents |
| **Culture** | organizational-psychologist | agency-agents |
| | specialized-cultural-intelligence-strategist | agency-agents |

```
🔁 Handoff → All Teams (talent distribution)
```

---

## 🏢 TEAM 20: Business Strategy & Operations Team
**মোট Member: ৩১** — যাচাইকৃত · ৯ ধাপ · ৩১ assignment

```
Workflow: Analysis → Strategy → Planning → M&A → Operations → Change Mgmt → Reporting
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Analysis** | business-analysis | roles.yaml |
| | business-strategist | agency-agents |
| | product-capability, product-lens | ecc |
| | ai-startup-trend-analysis-agent | awesome-llm |
| | ai-startup-insight-fire1-agent | awesome-llm |
| **Strategy** | startup-strategy | roles.yaml |
| | specialized-strategy-duel-agent | agency-agents |
| | specialized-master-plan-architect | agency-agents |
| | specialized-chief-of-staff, chief-of-staff-ecc | agency-agents, ecc |
| **Planning** | operations | roles.yaml |
| | operations-manager | agency-agents |
| | product-sprint-prioritizer | agency-agents |
| **M&A** | m-a-integration-manager | agency-agents |
| | ai-vc-due-diligence-agent-team | awesome-llm |
| **Supply Chain** | supply-chain-strategist | agency-agents |
| | inventory-demand-planning, production-scheduling | ecc |
| | logistics-exception-management | ecc |
| | returns-reverse-logistics | ecc |
| | carrier-relationship-management | ecc |
| | customs-trade-compliance | ecc |
| **Change Mgmt** | change-management-consultant | agency-agents |
| **Consulting** | ai-consultant-agent | awesome-llm |
| | government-digital-presales-consultant | agency-agents |
| | specialized-french-consulting-market | agency-agents |
| | specialized-korean-business-navigator | agency-agents |
| **Real Estate** | real-estate-buyer-seller | agency-agents |
| | ai-real-estate-agent-team | awesome-llm |
| **Energy** | energy-procurement | ecc |

```
🔁 Handoff → All Teams (strategic directives)
```
*(ঠিক করা হয়েছে: "Planning" ধাপে আগে "project-management-sprint-prioritizer → product-sprint-prioritizer" ভুল করে লেখা ছিল — সঠিক, একমাত্র role হলো product-sprint-prioritizer।)*

---

## 📝 TEAM 21: Documentation & Developer Experience Team
**মোট Member: ২৩** — যাচাইকৃত · ৮ ধাপ · ২৩ assignment

```
Workflow: Audit → Plan → Write → Review → Publish → Maintain → DX Optimize
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Audit** | docs-governance, living-docs-governance | ecc |
| | documentation-lookup, docs-lookup | ecc |
| **Plan** | developer-experience | roles.yaml |
| | product-dx-engineer | agency-agents |
| **Write** | docs, tutorial-writing | roles.yaml |
| | engineering-technical-writer | agency-agents |
| | engineering-universal-document-compiler | agency-agents |
| | specialized-document-generator | agency-agents |
| | article-writing | ecc |
| **UX Writing** | ux-writing | roles.yaml |
| **Code Tour** | code-tour, codebase-onboarding | ecc |
| | specialized-codebase-archaeologist | agency-agents |
| | commit-archaeologist | awesome-llm |
| **API Docs** | api-design-ecc | ecc |
| | contract-first | ecc |
| **Maintain** | doc-updater | ecc |
| | i18n-sync | ecc |
| **DX** | engineering-developer-tooling-engineer | agency-agents |
| | specialized-developer-advocate | agency-agents |

```
🔁 Handoff → All Engineering Teams (docs delivery)
```

---

## 🎨 TEAM 22: Design & Creative Team
**মোট Member: ৩৫** — যাচাইকৃত · ৮ ধাপ · ৩৫ assignment

```
Workflow: Research → UX → UI → Brand → Motion → Content → Review
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Research** | design-ux-researcher | agency-agents |
| | persona-walkthrough-specialist | agency-agents |
| **UX** | design-ux-architect | agency-agents |
| | design, accessibility | roles.yaml |
| | make-interfaces-feel-better | ecc |
| **UI** | design-ui-designer | agency-agents |
| | design-ui-finish-gate-reviewer | agency-agents |
| | design-system, liquid-glass-design | ecc |
| | taste, taste-application, taste-distillation | ecc |
| | ios-icon-gen | ecc |
| **Brand** | branding | roles.yaml |
| | design-brand-guardian | agency-agents |
| | brand-voice, brand-discovery | ecc |
| **Visuals** | illustration | roles.yaml |
| | design-inclusive-visuals-specialist | agency-agents |
| | design-visual-storyteller | agency-agents |
| | design-image-prompt-engineer | agency-agents |
| | design-whimsy-injector | agency-agents |
| **Motion** | motion-graphics | roles.yaml |
| | animations, text-animations, transitions | ecc |
| | motion-foundations, motion-advanced, motion-patterns | ecc |
| **Content** | content-writing, creative-writing | roles.yaml |
| **Multimodal Design** | multimodal-design-agent-team | awesome-llm |
| | multimodal-uiux-feedback-agent-team | awesome-llm |
| | ai-shadcn-component-generator | awesome-llm |

```
🔁 Handoff → Frontend Team (implementation) → QA (accessibility audit)
```

---

## 🎬 TEAM 23: Video & Media Production Team
**মোট Member: ৪৩** — যাচাইকৃত · ১২ ধাপ · ৪৩ assignment

```
Workflow: Script → Record/Capture → Edit → Motion/VFX → Audio → Captions → Render → Distribute
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Script** | video-script | roles.yaml |
| | ai-movie-production-agent | awesome-llm |
| **Capture** | extract-frames, get-video-dimensions | ecc |
| | get-video-duration, get-audio-duration | ecc |
| | can-decode | ecc |
| **Edit** | video-editing, trimming, sequencing | ecc |
| | videos, images, gifs | ecc |
| | remotion-video-creation | ecc |
| | tasteforge-video | ecc |
| **Motion/VFX** | motion-graphics | roles.yaml |
| | 3d, animations, text-animations | ecc |
| | transitions, lottie | ecc |
| | blender-motion-state-inspection | ecc |
| | manim-video | ecc |
| **Audio** | audio, ai-music-generator-agent | ecc, awesome-llm |
| | podcast-production | roles.yaml |
| **Captions** | display-captions, import-srt-captions | ecc |
| | transcribe-captions | ecc |
| **Metadata** | calculate-metadata, measuring-dom-nodes | ecc |
| | measuring-text, timing | ecc |
| **Fonts/Assets** | fonts, assets | ecc |
| **Compositions** | compositions | ecc |
| **Streaming** | engineering-video-streaming-engineer | agency-agents |
| | videodb | ecc |
| **AI Media** | fal-ai-media | ecc |
| | ai-meme-generator-agent-browseruse | awesome-llm |
| **Slides/Presentation** | presentation | roles.yaml |
| | frontend-slides | ecc |
| | ui-demo | ecc |

```
🔁 Handoff → Marketing Team (distribute) → Social Media
```

---

## 🏥 TEAM 24: Healthcare Engineering Team
**মোট Member: ১৮** — যাচাইকৃত · ৭ ধাপ · ১৮ assignment

```
Workflow: Requirements → Compliance Check → Build → Clinical Validate → Deploy → Monitor
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Requirements** | healthcare-innovation-strategist | agency-agents |
| | healthcare-sovereign-health-systems-agent | agency-agents |
| **Compliance** | hipaa-compliance, healthcare-phi-compliance | ecc |
| | healthcare-marketing-compliance-specialist | agency-agents |
| | data-privacy-officer | agency-agents |
| **Build** | healthcare-cdss-patterns | ecc |
| | healthcare-emr-patterns | ecc |
| | medical-billing-coding-specialist | agency-agents |
| **Clinical Validation** | healthcare-clinical-evidence-agent | agency-agents |
| | healthcare-reviewer | ecc |
| | healthcare-eval-harness | ecc |
| **AI/Imaging** | ai-medical-imaging-agent | awesome-llm |
| | ai-health-fitness-agent | awesome-llm |
| | ai-mental-wellbeing-agent | awesome-llm |
| **Services** | healthcare-customer-service | agency-agents |
| | healthcare-aging-parent-care-companion | agency-agents |
| **Wellness** | ai-recipe-meal-planning-agent | awesome-llm |

```
🔁 Handoff → Legal Team (compliance audit) → QA Team
```

---

## 🗺️ TEAM 25: GIS & Spatial Engineering Team
**মোট Member: ১৩** — যাচাইকৃত · ৮ ধাপ · ১৩ assignment

```
Workflow: Data Collect → Process → Analyze → Visualize → 3D/BIM → QA → Deploy
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Data Collect** | gis-spatial-data-engineer | agency-agents |
| | drone-reality-mapping-specialist | agency-agents |
| **Process** | gis-geoprocessing-specialist | agency-agents |
| | gis-spatial-data-scientist | agency-agents |
| **Analyze** | gis-analyst | agency-agents |
| | gis-geoai-ml-engineer | agency-agents |
| **Visualize** | gis-cartography-designer | agency-agents |
| | gis-3d-scene-developer | agency-agents |
| **BIM** | gis-bim-specialist | agency-agents |
| **Web** | gis-web-gis-developer | agency-agents |
| **QA** | gis-qa-engineer | agency-agents |
| **Consulting** | gis-technical-consultant | agency-agents |
| | gis-solution-engineer | agency-agents |

```
🔁 Handoff → Product Team → Engineering Team (integration)
```

---

## 🔧 TEAM 26: Language-Specific Code Review Team
**মোট Member: ৩০** — যাচাইকৃত · ২ ধাপ · ৩০ assignment

```
Workflow: PR Submitted → Language Review → Build Resolve → Style Check → Approve/Reject
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Review** | cpp-reviewer, csharp-reviewer | ecc |
| | django-reviewer, fastapi-reviewer, flutter-reviewer | ecc |
| | fsharp-reviewer, go-reviewer, java-reviewer | ecc |
| | kotlin-reviewer, php-reviewer, python-reviewer | ecc |
| | react-reviewer, ruby-reviewer, rust-reviewer | ecc |
| | swift-reviewer, typescript-reviewer, vue-reviewer | ecc |
| | bengali-reviewer | ecc |
| **Build Resolve** | cpp-build-resolver, django-build-resolver | ecc |
| | go-build-resolver, java-build-resolver | ecc |
| | kotlin-build-resolver, pytorch-build-resolver | ecc |
| | react-build-resolver, rust-build-resolver | ecc |
| | swift-build-resolver, dart-build-resolver | ecc |
| | harmonyos-app-resolver, build-error-resolver | ecc |

```
🔁 Handoff → CI/CD Pipeline → QA Team
```

---

## 🛠️ TEAM 27: CMS & Web Platform Team
**মোট Member: ১৫** — যাচাইকৃত · ৪ ধাপ · ১৫ assignment

```
Workflow: Setup → Develop → Optimize → Secure → Launch
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Setup** | wordpress, engineering-cms-developer | roles.yaml, agency-agents |
| | engineering-uswds-developer | agency-agents |
| **Develop** | php, engineering-drupal-performance | roles.yaml, agency-agents |
| | engineering-drupal-shopping-cart | agency-agents |
| | engineering-wordpress-performance | agency-agents |
| | engineering-wordpress-shopping-cart | agency-agents |
| | laravel-patterns, laravel-plugin-discovery | ecc |
| **Optimize** | web-performance | roles.yaml |
| | laravel-security, laravel-tdd, laravel-verification | ecc |
| **Secure** | perl-security | ecc |

```
🔁 Handoff → SEO Team → Marketing Team
```

---

## 🌐 TEAM 28: Internationalization & Localization Team
**মোট Member: ১২** — যাচাইকৃত · ৪ ধাপ · ১২ assignment

```
Workflow: Audit → Extract → Translate → Adapt → Validate → Deploy
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Audit** | localization-engineering | roles.yaml |
| | i18n-sync | ecc |
| | internationalization-engineer | agency-agents |
| **Translate** | translation, language-translator | roles.yaml, agency-agents |
| | visa-doc-translate | ecc |
| | bengali-nlp | ecc |
| **Cultural Adapt** | specialized-cultural-intelligence-strategist | agency-agents |
| | marketing-china-market-localization-strategist | agency-agents |
| | specialized-korean-business-navigator | agency-agents |
| | specialized-french-consulting-market | agency-agents |
| **Content** | editor | roles.yaml |

```
🔁 Handoff → QA Team → Marketing Team (localized content)
```

---

## 🤝 TEAM 29: Project Management & Ops Team
**মোট Member: ১৯** — যাচাইকৃত · ৬ ধাপ · ১৯ assignment

```
Workflow: Intake → Plan → Sprint → Track → Report → Retrospective
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Intake** | product-management | roles.yaml |
| | project-manager-senior | agency-agents |
| | project-management-project-shepherd | agency-agents |
| **Plan** | product-sprint-prioritizer | agency-agents |
| | plan-canvas, plan-orchestrate | ecc |
| | writing-plans, executing-plans | superpowers |
| **Sprint** | project-management-jira-workflow-steward | agency-agents |
| | jira-integration | ecc |
| | project-flow-ops | ecc |
| **Track** | project-management-experiment-tracker | agency-agents |
| | story-lifecycle | ecc |
| | project-management-meeting-notes-specialist | agency-agents |
| | ai-meeting-agent | awesome-llm |
| **Studio Ops** | project-management-studio-operations | agency-agents |
| | project-management-studio-producer | agency-agents |
| **Research** | research-synthesist | agency-agents |
| | research-ops | ecc |

```
🔁 Handoff → All Teams (coordination)
```

---

## 🧠 TEAM 30: Agent Orchestration & Meta-AI Team
**মোট Member: ১০৮** — যাচাইকৃত · ১৪ ধাপ · ১০৮ assignment

```
Workflow: Design → Build → Orchestrate → Evaluate → Secure → Optimize → Scale
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Design** | agent-architecture-audit | ecc |
| | agentic-engineering, agentic-os | ecc |
| | ai-first-engineering | ecc |
| | blueprint | ecc |
| | specialized-workflow-architect | agency-agents |
| | specialized-mcp-builder | agency-agents |
| **Build** | engineering-prompt-engineer | agency-agents |
| | engineering-rag-pipeline-engineer | agency-agents |
| | engineering-multi-agent-systems-architect | agency-agents |
| | engineering-orgscript-engineer | agency-agents |
| | agent-introspection-debugging | ecc |
| | autonomous-agent-harness | ecc |
| | deepseek-harness-setup | ecc |
| | eval-harness, gan-style-harness | ecc |
| | nanoclaw-repl | ecc |
| | openclaw-persona-forge | ecc |
| | agent-harness-construction | ecc |
| **MCP** | ai-mcp-app-builder | awesome-llm |
| | mcp-server-patterns, mcp-dependency-review | ecc |
| | mcp-use-server, mcp-apps-generative-ui-showcase | awesome-llm |
| | browser-mcp-agent, github-mcp-agent | awesome-llm |
| | multi-mcp-agent, multi-mcp-agent-router | awesome-llm |
| | notion-mcp-agent, openai-remote-mcp-bridge | awesome-llm |
| | codehealth-mcp | ecc |
| **Orchestrate** | agents-orchestrator | agency-agents |
| | orch-add-feature, orch-build-mvp | ecc |
| | orch-change-feature, orch-fix-defect | ecc |
| | orch-refine-code, orch-pipeline | ecc |
| | team-agent-orchestration, team-builder | ecc |
| | dev-team, dmux-workflows | ecc |
| | council, council-multi-model | ecc |
| | ecc-council | ecc (command) |
| | dynamic-workflow-mode | ecc |
| | operator-approval-loop | ecc |
| | advisor-orchestrator-worker | awesome-llm |
| **Parallel** | parallel-execution-optimizer | ecc |
| | dispatching-parallel-agents | superpowers |
| | subagent-driven-development | superpowers |
| **Memory** | unified-memory | ecc |
| | llm-app-personalized-memory | awesome-llm |
| | ai-arxiv-agent-memory, ai-travel-agent-memory | awesome-llm |
| | adk-career-coach-agent-memory | awesome-llm |
| **Evaluate** | agent-eval, agent-self-evaluation | ecc |
| | agent-sort | ecc |
| | testing-reality-checker | agency-agents |
| | specialized-model-qa | agency-agents |
| | agent-evaluator, harness-optimizer | ecc |
| **Scale** | enterprise-agent-ops | ecc |
| **Secure** | agent-security-hardening | ecc |
| | ai-agent-governance | awesome-llm |
| | multi-agent-trust-layer, trust-gated-agent-team | awesome-llm |
| | agentic-identity-trust-architect | agency-agents |
| **Optimize** | prompt-optimizer, prompt-caching-strategy | ecc |
| | token-budget-advisor, token-card | ecc |
| | cost-aware-llm-pipeline, cost-tracking | ecc |
| | context-budget | ecc |
| | regex-vs-llm-structured-text | ecc |
| | iterative-retrieval | ecc |
| **Loops** | autonomous-loops, continuous-agent-loop | ecc |
| | loop-design-check, loop-operator | ecc |
| | verification-loop | ecc |
| **Skills Mgmt** | skill-create, skill-health | ecc (commands) |
| | skill-comply, skill-scout, skill-stocktake | ecc |
| | skill-host-compat | ecc |
| **Config** | configure-ecc, ecc-conventions, ecc-guide | ecc |
| | ecc-recipes | ecc |
| | hookify-rules, rules-distill | ecc |
| | hermes-imports | ecc |
| | config-gc | ecc |
| **Meta** | using-superpowers, diagnosing-superpowers | superpowers |
| | brainstorming, writing-skills | superpowers |
| | strategic-compact | ecc |
| | context-and-decisions | ecc |
| | recursive-decision-ledger | ecc |
| | observer | ecc |
| | continuous-learning, continuous-learning-v2 | ecc |
| | search-first | ecc |

```
🔁 Handoff → সব Team-কে Agent capability provide করে
```

---

## 🔄 TEAM 31: Reverse Engineering & Legacy Team
**মোট Member: ১৯** — যাচাইকৃত · ৫ ধাপ · ১৯ assignment

```
Workflow: Discovery → Analyze → Document → Modernize → Verify
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Discovery** | reverse-engineer-anything | rea |
| | rea-analysis-optimization | rea |
| | specialized-codebase-archaeologist | agency-agents |
| | code-explorer | ecc |
| **Analyze** | rea-tool-design | rea |
| | type-design-analyzer | ecc |
| | comment-analyzer, conversation-analyzer | ecc |
| | pr-test-analyzer | ecc |
| **Document** | rea-changelog-update | rea |
| | code-tour | ecc |
| **Modernize** | legacy-modernization | roles.yaml |
| | refactoring | roles.yaml |
| | refactor-cleaner, code-simplifier | ecc |
| | inherit-legacy-style | ecc |
| | brownfield-retrospective | ecc |
| | ai-codebase-migration-agent | awesome-llm |
| **Verify** | rea-ci-tests-audit | rea |

```
🔁 Handoff → Engineering Team (modernized codebase)
```

---

## 📦 TEAM 32: Open Source & Community Team
**মোট Member: ৯** — যাচাইকৃত · ৬ ধাপ · ৯ assignment

```
Workflow: Scout → Fork → Sanitize → Package → Publish → Community
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Scout** | repo-scan | ecc |
| | skill-scout | ecc |
| **Fork** | opensource-forker | ecc |
| **Sanitize** | opensource-sanitizer | ecc |
| **Package** | opensource-packager | ecc |
| **Pipeline** | opensource-pipeline | ecc |
| **Community** | marketing-developer-community-builder | agency-agents |
| | specialized-developer-advocate | agency-agents |
| | marketing-reddit-community-builder | agency-agents |

```
🔁 Handoff → Documentation Team → Marketing Team
```

---

## ☁️ TEAM 33: China & APAC Tech Ecosystem Team
**মোট Member: ১৮** — যাচাইকৃত · ৪ ধাপ · ১৮ assignment

```
Workflow: Market Research → Platform Adapt → Content → Launch → Optimize
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Research** | marketing-china-market-localization-strategist | agency-agents |
| | specialized-korean-business-navigator | agency-agents |
| **Platform** | engineering-china-network-engineer | agency-agents |
| | engineering-feishu-integration-developer | agency-agents |
| | engineering-wechat-mini-program-developer | agency-agents |
| | engineering-gaussdb-expert | agency-agents |
| **Content** | marketing-baidu-seo-specialist | agency-agents |
| | marketing-bilibili-content-strategist | agency-agents |
| | marketing-douyin-strategist | agency-agents |
| | marketing-kuaishou-strategist | agency-agents |
| | marketing-weibo-strategist | agency-agents |
| | marketing-wechat-official-account | agency-agents |
| | marketing-xiaohongshu-specialist | agency-agents |
| | marketing-zhihu-strategist | agency-agents |
| **Commerce** | cross-border-e-commerce-specialist | agency-agents |
| | marketing-china-ecommerce-operator | agency-agents |
| | marketing-livestream-commerce-coach | agency-agents |
| | marketing-private-domain-operator | agency-agents |

```
🔁 Handoff → Sales Team → Business Strategy
```

---

## 💻 TEAM 34: Language-Specific Engineering Teams

**সাব-টিম মোট: ৭৫ role** — ১০টি sub-team (34A–34J), `scripts/build_teams.py --stats`-এ প্রতিটির আলাদা সংখ্যা দেখা যায়।

### 34A: Rust Team (৭)
```
Workflow: Design → Code → Review → Test → Build Resolve → Refactor
```
| Role | ফাইল |
|---|---|
| rust | roles.yaml |
| rust-patterns, rust-testing | ecc |
| rust-reviewer, rust-build-resolver | ecc |
| engineering-rust-refactoring-specialist | agency-agents |
| engineering-webassembly-engineer | agency-agents |

### 34B: Python Team (১৪)
```
Workflow: Design → Code → Review → Test → Build Resolve → Deploy
```
| Role | ফাইল |
|---|---|
| python | roles.yaml |
| python-patterns, python-testing | ecc |
| python-reviewer | ecc |
| django-patterns, django-security, django-tdd, django-verification | ecc |
| django-reviewer, django-build-resolver | ecc |
| django-celery | ecc |
| fastapi-patterns, fastapi-reviewer | ecc |
| generating-python-installer | ecc |

### 34C: Java/Kotlin/JVM Team (২১)
```
Workflow: Design → Code → Review → Test → Build Resolve → Deploy
```
| Role | ফাইল |
|---|---|
| java | roles.yaml |
| java-coding-standards, java-reviewer, java-build-resolver | ecc |
| jpa-patterns, mybatis-patterns | ecc |
| springboot-patterns, springboot-security, springboot-tdd, springboot-verification | ecc |
| kotlin-patterns, kotlin-coroutines-flows, kotlin-exposed-patterns | ecc |
| kotlin-ktor-patterns, kotlin-testing | ecc |
| kotlin-reviewer, kotlin-build-resolver | ecc |
| quarkus-patterns, quarkus-security, quarkus-tdd, quarkus-verification | ecc |

### 34D: Go Team (৫)
| Role | ফাইল |
|---|---|
| golang | roles.yaml |
| golang-patterns, golang-testing | ecc |
| go-reviewer, go-build-resolver | ecc |

### 34E: C++/Systems Team (৪)
| Role | ফাইল |
|---|---|
| cpp-coding-standards, cpp-testing | ecc |
| cpp-reviewer, cpp-build-resolver | ecc |

### 34F: .NET/C#/F# Team (৬)
| Role | ফাইল |
|---|---|
| dotnet | roles.yaml |
| dotnet-patterns | ecc |
| csharp-reviewer, csharp-testing | ecc |
| fsharp-reviewer, fsharp-testing | ecc |

### 34G: Ruby/Rails Team (৩)
| Role | ফাইল |
|---|---|
| ruby | roles.yaml |
| ruby-reviewer | ecc |
| rails-patterns | ecc |

### 34H: PHP/Laravel Team (৭)
| Role | ফাইল |
|---|---|
| php | roles.yaml |
| php-reviewer | ecc |
| laravel-patterns, laravel-security, laravel-tdd, laravel-verification | ecc |
| laravel-plugin-discovery | ecc |

### 34I: Elixir/Perl/Other Team (৪)
| Role | ফাইল |
|---|---|
| elixir | roles.yaml |
| perl-patterns, perl-security, perl-testing | ecc |

### 34J: Specialized Platform Team (৪)
| Role | ফাইল |
|---|---|
| engineering-servicenow-developer-mentor | agency-agents |
| specialized-salesforce-architect | agency-agents |
| tinystruct-patterns | ecc |
| bun-runtime | ecc |

*(ঠিক করা হয়েছে: আগে "engineering-salesforce-architect → specialized-salesforce-architect" ভুল করে লেখা ছিল — সঠিক, একমাত্র role হলো specialized-salesforce-architect।)*

---

## 🔊 TEAM 35: Support & Customer Operations Team
**মোট Member: ১১** — যাচাইকৃত · ৬ ধাপ · ১১ assignment

```
Workflow: Ticket In → Triage → Respond → Escalate → Resolve → Feedback
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Intake** | support-engineering | roles.yaml |
| | customer-service | agency-agents |
| | ai-customer-support-agent | awesome-llm |
| **Respond** | support-support-responder | agency-agents |
| | customer-support-voice-agent | awesome-llm |
| **Hospitality** | hospitality-guest-services | agency-agents |
| | retail-customer-returns | agency-agents |
| **Customer Success** | customer-success-manager | agency-agents |
| **Analytics** | support-analytics-reporter | agency-agents |
| | support-executive-summary-generator | agency-agents |
| **Infrastructure** | support-infrastructure-maintainer | agency-agents |

```
🔁 Handoff → Product Team (feedback) → Engineering (bug fixes)
```

---

## ✍️ TEAM 36: Research & Academic Team
**মোট Member: ২০** — যাচাইকৃত · ৬ ধাপ · ২০ assignment

```
Workflow: Literature Review → Hypothesis → Data Collect → Analyze → Write → Peer Review → Publish
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Literature** | research | roles.yaml |
| | scientific-thinking-literature-review | ecc |
| | scientific-thinking-scholar-evaluation | ecc |
| | deep-research, exa-search | ecc |
| **Domain Expertise** | academic-anthropologist | agency-agents |
| | academic-geographer | agency-agents |
| | academic-historian | agency-agents |
| | academic-narratologist | agency-agents |
| | academic-psychologist | agency-agents |
| | academic-statistician | agency-agents |
| **Data** | scientific-db-pubmed-database | ecc |
| | scientific-db-uspto-database | ecc |
| | scientific-pkg-gget | ecc |
| **Research Agents** | ag2-adaptive-research-team | awesome-llm |
| | research-agent-gemini-interaction-api | awesome-llm |
| | llm-panel-agent-team | awesome-llm |
| **Writing** | grant-writing | roles.yaml |
| | grant-writer | agency-agents |
| **Analysis** | ai-reasoning-agent | awesome-llm |

```
🔁 Handoff → Product Team (applied research)
```

---

## 📲 TEAM 37: Communication & Workspace Ops Team
**মোট Member: ১৪** — যাচাইকৃত · ৭ ধাপ · ১৪ assignment

```
Workflow: Setup → Integrate → Automate → Monitor → Report
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Workspace** | google-workspace-ops | ecc |
| | workspace-surface-audit | ecc |
| **Email** | email-ops, engineering-email-intelligence-engineer | ecc, agency-agents |
| | mailtrap-email-integration | ecc |
| **Messages** | messages-ops | ecc |
| | unified-notifications-ops | ecc |
| **Knowledge** | knowledge-ops | ecc |
| | engineering-knowledge-graph-engineer | agency-agents |
| **Automation** | spreadsheet-automation | roles.yaml |
| | automation-audit-ops | ecc |
| | automation-governance-architect | agency-agents |
| **IT** | engineering-it-service-manager | agency-agents |
| **Social** | x-api | ecc |

```
🔁 Handoff → All Teams (communication infrastructure)
```

---

## 🏠 TEAM 38: Lifestyle & Personal AI Team
**মোট Member: ১৫** — যাচাইকৃত · ৬ ধাপ · ১৫ assignment

```
Workflow: Need Identify → Plan → Execute → Monitor → Adapt
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Health** | ai-health-fitness-agent | awesome-llm |
| | ai-mental-wellbeing-agent | awesome-llm |
| | ai-breakup-recovery-agent | awesome-llm |
| **Home** | ai-home-renovation-agent | awesome-llm |
| **Food** | ai-recipe-meal-planning-agent | awesome-llm |
| **Travel** | ai-travel-agent-memory | awesome-llm |
| | ai-travel-planner-mcp-agent-team | awesome-llm |
| | ai-audio-tour-agent | awesome-llm |
| **Air Quality** | ai-aqi-analysis-agent | awesome-llm |
| **General** | general, assistant | roles.yaml |
| | local-worker, local-heavy, local-chat | roles.yaml |
| | uncensored-local | roles.yaml |

```
🔁 Self-contained — ব্যক্তিগত ব্যবহার
```

---

## 📰 TEAM 39: Journalism & Content Intelligence Team
**মোট Member: ১০** — যাচাইকৃত · ৬ ধাপ · ১০ assignment

```
Workflow: Monitor → Investigate → Write → Edit → Publish → Distribute
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Monitor** | always-on-hn-briefing-agent | awesome-llm |
| | release-radar-agent | awesome-llm |
| | product-launch-intelligence-agent | awesome-llm |
| **Investigate** | ai-journalist-agent | awesome-llm |
| | osint-investigation | ecc |
| **Write** | content-writing, creative-writing | roles.yaml |
| **Edit** | editor | roles.yaml |
| **Blog** | ai-blog-search | awesome-llm |
| **Podcast** | ai-blog-to-podcast-agent | awesome-llm |

```
🔁 Handoff → Marketing Team → Social Media
```

---

## 🏗️ TEAM 40: Enterprise Integration & ServiceNow Team
**মোট Member: ১২** — যাচাইকৃত · ৬ ধাপ · ১২ assignment

```
Workflow: Assess → Design → Build → Integrate → Test → Deploy
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Assess** | engineering-servicenow-developer-mentor | agency-agents |
| | specialized-salesforce-architect | agency-agents |
| **Build** | engineering-realtime-collaboration-engineer | agency-agents |
| | engineering-rapid-prototyper | agency-agents |
| **Document** | engineering-pdf-engine-architect | agency-agents |
| | esign-field-placement | ecc |
| | nutrient-document-processing | ecc |
| **Search** | search-engineering | roles.yaml |
| | engineering-search-relevance-engineer | agency-agents |
| **Notifications** | notifications | roles.yaml |
| **Payments** | payments | roles.yaml |
| | engineering-payments-billing-engineer | agency-agents |

```
🔁 Handoff → Platform Team → Support Team
```

---

## 🔬 TEAM 41: Dev Workflow Superpowers Team
**মোট Member: ৩৩** — যাচাইকৃত · ৮ ধাপ · ৩৩ assignment

```
Workflow: Plan → Code → Review → Debug → Test → Verify → Ship
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Plan** | brainstorming | superpowers |
| | writing-plans | superpowers |
| | executing-plans | superpowers |
| **Code** | using-superpowers, writing-skills | superpowers |
| | coding-standards | ecc |
| | intent-driven-development | ecc |
| | santa-method | ecc |
| | engineering-minimal-change-engineer | agency-agents |
| **Git** | using-git-worktrees | superpowers |
| | finishing-a-development-branch | superpowers |
| | git-workflow, github-ops | ecc |
| | engineering-git-workflow-master | agency-agents |
| **Review** | requesting-code-review | superpowers |
| | receiving-code-review | superpowers |
| | code-review, engineering-code-reviewer | roles.yaml, agency-agents |
| | commit-reviewer, plankton-code-quality | ecc |
| | change-impact | ecc |
| **Debug** | systematic-debugging | superpowers |
| | diagnosing-superpowers | superpowers |
| | debugging | roles.yaml |
| | silent-failure-hunter | ecc |
| | error-handling | ecc |
| **Test** | test-driven-development | superpowers |
| | tdd-guide, tdd-workflow | ecc |
| **Verify** | verification-before-completion | superpowers |
| | verification-loop | ecc |
| **Parallel** | dispatching-parallel-agents | superpowers |
| | subagent-driven-development | superpowers |

```
🔁 Handoff → সব Engineering Teams-এ methodology inject করে
```

---

## 📊 TEAM 42: GAN & Generative AI Team
**মোট Member: ৬** — যাচাইকৃত · ৩ ধাপ · ৬ assignment

```
Workflow: Plan → Generate → Evaluate → Iterate
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Plan** | gan-planner | ecc |
| **Generate** | gan-generator | ecc |
| | ai-shadcn-component-generator | awesome-llm |
| | generative-ui-starter-project | awesome-llm |
| **Evaluate** | gan-evaluator | ecc |
| | gan-style-harness | ecc |

```
🔁 Handoff → Design Team → Frontend Team
```

---

## ⚡ TEAM 43: Performance & Optimization Team
**মোট Member: ১২** — যাচাইকৃত · ৩ ধাপ · ১২ assignment

```
Workflow: Benchmark → Profile → Optimize → Validate → Monitor
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Benchmark** | benchmark, benchmark-methodology | ecc |
| | benchmark-optimization-loop | ecc |
| | performance-testing | roles.yaml |
| **Profile** | performance-optimizer | ecc |
| | web-performance | roles.yaml |
| | react-performance | ecc |
| | latency-critical-systems | ecc |
| **Optimize** | data-throughput-accelerator | ecc |
| | content-hash-cache-pattern | ecc |
| | engineering-filament-optimization-specialist | agency-agents |
| | engineering-autonomous-optimization-architect | agency-agents |

```
🔁 Handoff → Engineering Teams → Platform Team
```

---

## 🧮 TEAM 44: Supply Chain & Logistics Team
**মোট Member: ৯** — যাচাইকৃত · ৬ ধাপ · ৯ assignment

```
Workflow: Demand Plan → Source → Produce → Ship → Track → Returns
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Demand** | inventory-demand-planning | ecc |
| **Source** | carrier-relationship-management | ecc |
| | counterparty-channel-discipline | ecc |
| **Produce** | production-scheduling | ecc |
| | quality-nonconformance | ecc |
| **Ship** | logistics-exception-management | ecc |
| | customs-trade-compliance | ecc |
| **Returns** | returns-reverse-logistics | ecc |
| **Strategy** | supply-chain-strategist | agency-agents |

```
🔁 Handoff → Finance Team (billing) → Operations Team
```

---

## 🔍 TEAM 45: Search & Discovery Engineering Team
**মোট Member: ৫** — যাচাইকৃত · ৪ ধাপ · ৫ assignment

```
Workflow: Index → Relevance → Personalize → Monitor
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Index** | search-engineering | roles.yaml |
| | lsp-index-engineer | agency-agents |
| **Relevance** | engineering-search-relevance-engineer | agency-agents |
| **Personalize** | recsys-pipeline-architect | ecc |
| **Tools** | ai-blog-search | awesome-llm |

```
🔁 Handoff → Product Team → Frontend Team
```

---

## 🎶 TEAM 46: Generative UI & MCP Apps Team
**মোট Member: ৯** — যাচাইকৃত · ৪ ধাপ · ৯ assignment

```
Workflow: Design → Build → MCP Connect → Deploy → Showcase
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Design** | generative-ui-starter-project | awesome-llm |
| | ai-shadcn-component-generator | awesome-llm |
| **Build** | ai-mcp-app-builder | awesome-llm |
| | mcp-apps-generative-ui-showcase | awesome-llm |
| **MCP** | threejs-server, web | awesome-llm |
| | mcp-server-patterns | ecc |
| **Dev Agents** | devpulse-ai | awesome-llm |
| | multimodal-coding-agent-team | awesome-llm |

```
🔁 Handoff → Frontend Team → Design Team
```

---

## 🚀 TEAM 47: ECC Command & Evolution Team
**মোট Member: ১৭** — যাচাইকৃত · ৮ ধাপ · ১৭ assignment

```
Workflow: Evolve → Export → Import → Status → Promote → Prune → Council
```

| ধাপ | Role(s) | ফাইল |
|---|---|---|
| **Evolve** | evolve | ecc (command) |
| **Instinct** | instinct-export, instinct-import, instinct-status | ecc (commands) |
| **Projects** | projects | ecc (command) |
| **Promote** | promote | ecc (command) |
| **Prune** | prune | ecc (command) |
| **Skills** | skill-create, skill-health | ecc (commands) |
| **Council** | ecc-council | ecc (command) |
| **Guide** | ecc-guide, ecc-conventions, ecc-recipes | ecc |
| | claude-devfleet, ck | ecc |
| | nasiko-control-plane | ecc |
| | ralphinho-rfc-pipeline | ecc |

```
🔁 Self-referential — নিজেই নিজেকে improve করে
```

---

## 🔗 TEAM 48: Remaining Specialists (Uncategorized/Cross-Cutting)
**মোট Member: ৯** — যাচাইকৃত · ১ ধাপ · ৯ assignment

| Role | ফাইল | প্রাসঙ্গিক Team |
|---|---|---|
| ai-services-agency | awesome-llm | Meta-AI |
| specialized-civil-engineer | agency-agents | Business Strategy |
| specialized-focus-music-architect | agency-agents | Lifestyle |
| zk-steward | agency-agents | Blockchain |
| identity-graph-operator | agency-agents | Agent Orchestration |
| scope-creep-detector | awesome-llm | Project Mgmt |
| first-reader | awesome-llm | Documentation |
| project-graveyard | awesome-llm | Project Mgmt |
| thinking-out-loud | awesome-llm | Superpowers |

---

# 🗺️ সম্পূর্ণ Team-to-Team Handoff Diagram

```
                              ┌─────────────────┐
                              │  Team 30: Agent  │
                              │  Orchestration   │
                              │  (Meta-AI Hub)   │
                              └────────┬─────────┘
                                       │ provides capabilities to ALL
                    ┌──────────────────┼──────────────────┐
                    │                  │                   │
              ┌─────▼──────┐   ┌──────▼──────┐   ┌──────▼──────┐
              │  Team 41:  │   │  Team 47:   │   │  Team 42:   │
              │ Superpowers│   │ ECC Command │   │  GAN/Gen AI │
              │  Workflow  │   │  Evolution  │   │             │
              └─────┬──────┘   └─────────────┘   └─────────────┘
                    │ methodology injection
    ┌───────────────┼───────────────────────────────────┐
    │               │               │                    │
┌───▼────┐   ┌─────▼──────┐  ┌────▼─────┐  ┌──────────▼────────┐
│Team 5: │   │  Team 8:   │  │ Team 10: │  │ Team 11: Mobile   │
│Fullstack│   │  AI/ML     │  │ Game Dev │  │ Team 12: Desktop  │
│Product │   │  + Team 9  │  │ + XR     │  │ Team 13: Blockchain│
│Eng.    │   │  NLP/CV    │  │          │  │ Team 34: Languages │
└───┬────┘   └─────┬──────┘  └────┬─────┘  └──────────┬────────┘
    │               │              │                    │
    ├───────────────┼──────────────┼────────────────────┤
    │               │              │                    │
┌───▼─────────────────────────────────────────────────▼──┐
│              Team 26: Language Code Review              │
│              Team 7: QA & Testing                       │
│              Team 43: Performance                       │
└───┬─────────────────────────────────────────────────┬──┘
    │                                                  │
┌───▼────────┐                                  ┌─────▼──────┐
│ Team 3:    │◄─────────────────────────────────►│ Team 6:    │
│ DevSecOps  │                                   │ Platform   │
│ Pipeline   │                                   │ Infra      │
└───┬────────┘                                   └─────┬──────┘
    │                                                   │
┌───▼─────────────────────────────────────────────────▼──┐
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ Team 1:  │  │ Team 2:  │  │ Team 4:  │   SECURITY   │
│  │ Red Team │─►│ Blue Team│◄─│ Vuln     │   LAYER      │
│  │          │  │          │  │ Research  │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│                                                         │
└─────────────────────────────────────────────────────────┘
    │
    │  outputs feed into
    ▼
┌────────────────────────────────────────────────────────┐
│                    BUSINESS LAYER                       │
│                                                        │
│  Team 15: Marketing    Team 16: Sales                  │
│  Team 17: Finance      Team 18: Legal                  │
│  Team 19: HR           Team 20: Business Strategy      │
│  Team 29: Project Mgmt Team 33: China/APAC             │
│  Team 35: Support      Team 44: Supply Chain           │
│                                                        │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│                   CONTENT LAYER                        │
│                                                        │
│  Team 21: Documentation  Team 22: Design/Creative      │
│  Team 23: Video/Media    Team 28: i18n/L10n            │
│  Team 36: Research       Team 39: Journalism           │
│                                                        │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│               DOMAIN SPECIALIST LAYER                  │
│                                                        │
│  Team 24: Healthcare   Team 25: GIS/Spatial            │
│  Team 27: CMS/Web      Team 31: Reverse Eng.           │
│  Team 32: Open Source  Team 37: Comm/Workspace         │
│  Team 38: Lifestyle    Team 40: Enterprise             │
│  Team 45: Search       Team 46: Gen UI/MCP             │
│                                                        │
└────────────────────────────────────────────────────────┘
```

---

# ⚙️ Runtime: এই roster দিয়েই role ম্যানেজ হয়

এই ডকুমেন্টটাই শুধু কাগজ নয় — একই টেবিল থেকে `teams.yaml` তৈরি হয়, আর Nexus সেটাই পড়ে।
তাই "ডকুমেন্টে যা লেখা" আর "অ্যাপ যা জানে" একই থাকে; কোনোটাই হাতে বদলানো যায় না।

```bash
python3 scripts/build_teams.py --yaml    # docs/teams.md  ->  teams.yaml (generated)
python3 scripts/build_teams.py --check   # ডকুমেন্ট ⇄ teams.yaml ⇄ role packs — তিনটাই মিলছে কি না
```

| কমান্ড | কী করে |
|---|---|
| `nexus teams` | ৪৮টি team + ১০টি language sub-team, member ও ধাপ সংখ্যা সহ |
| `nexus teams show red-team` | workflow লাইন + প্রতিটি ধাপের role (`--stage Scoping`) |
| `nexus teams show 34a` | number, id বা title — যেভাবেই খুঁজুন |
| `nexus teams role bengali-nlp` | role-টি কোন কোন team-এর কোন ধাপে আছে |
| `nexus teams check` | রোস্টার বনাম ইনস্টল করা role: অজানা নাম, ভাঙা handoff/parent/child |
| `nexus roles --team qa-testing-team` | সাধারণ role লিস্টে team ফিল্টার |
| `nexus doctor` | `teams` লাইনে ৪৮ team / ১০০০ role দেখায় |

`nexus teams check` যা কঠোরভাবে ধরে (exit 1):
`docs/teams.md`-এর কোনো role নাম `roles.yaml` বা `roles.d/*.yaml`-এ নেই · handoff এমন team-এ
যায় যা রোস্টারে নেই · parent/child ভাঙা · কোনো team-এ কোনো stage নেই।

লেয়ারিং `roles.yaml`-এর মতোই: bundled → repo checkout → `$NEXUS_HOME` → `./.nexus/teams.yaml`
→ `./teams.yaml`। অর্থাৎ প্রজেক্টে নিজের team যোগ করা যায়, repo-র ৪৮টি team রয়ে যায়।
প্রজেক্টে যোগ করা নতুন role কোনো team-এ না থাকলে `check` শুধু *note* দেয় (error নয়), কারণ
সেটা বৈধ — কিন্তু shipped ১০০০ role-এর জন্য coverage ১০০০/১০০০ থাকতেই হবে।

**সীমা:** এটা grouping/management — routing আগের মতোই role-ভিত্তিক, execution
`pipelines.yaml` + approval gate দিয়ে, আর এতে কোনো নতুন autonomy যোগ হয় না। `nexus teams`
কোনো model কল করে না, কোনো টুল চালায় না।

---

# ✅ Verification & Changelog

## যাচাই কীভাবে হয়

```bash
python3 scripts/build_teams.py            # validate + এক নজরে stats
python3 scripts/build_teams.py --check    # CI মোড: কোনো ভুল থাকলে exit 1
python3 scripts/build_teams.py --write    # "মোট Member" সংখ্যা নতুন করে গোনে লেখে
python3 scripts/build_teams.py --stats    # প্রতি team-এর সংখ্যা + pack mix
```

**Error (build ভাঙে):** অজানা role · role ভুল pack-এ দাবি করা · অজানা pack · `Handoff → Team N`-এর
গন্তব্য ডকুমেন্টে নেই · কোনো pack-এর role কোনো team-এ নেই।

**Advisory (শুধু warning):** একই team-এ একই role দুইবার (যেমন `ai-safety` Research ও Evaluation-এ) ·
`Workflow:` লাইনের কোনো ধাপের আলাদা row নেই — workflow লাইনটা সারমর্ম, তাই কিছু ধাপ
(যেমন Team 9-এ Model Train/Fine-tune) একাধিক role-এর সাধারণ নাম, আলাদা row ছাড়াই থাকে।

## এই রাউন্ডে যা ঠিক হলো

| কী | আগে | এখন |
|---|---|---|
| Member সংখ্যা | "আনুমানিক", row গোনা হয়নি | প্রতিটি team/sub-team-এর exact unique count |
| Coverage | ৯৭৮ / ১০০০ | ১০০০ / ১০০০ |
| Team 26 | `dart-build-resolver` ভুলভাবে Review ধাপে | শুধু Build Resolve ধাপে |
| Team 30 | workflow-এ `Scale` ধাপ, table-এ row নেই | `enterprise-agent-ops` সহ **Scale** row |
| Team 1 | `purple-team-coordinator` কোনো team-এ ছিল না | নতুন **Purple Team** ধাপ (Retest-এর পরে) |
| Team 6 | `engineering-cloud-security-architect` (security-practitioners) | `security-cloud-security-architect` (agency-agents) |
| Team 20 | `project-management-sprint-prioritizer` | `product-sprint-prioritizer` |
| Team 34J | `engineering-salesforce-architect` | `specialized-salesforce-architect` |

## নতুন যোগ হওয়া ২২টি role (আগের ডকুমেন্টে কোনো team-এ ছিল না)

| Role | Pack | Team · ধাপ |
|---|---|---|
| purple-team-coordinator | security-practitioners | Team 1 · Purple Team |
| engineering-senior-developer | agency-agents | Team 5 · Frontend |
| fullstack | roles.yaml | Team 5 · Backend |
| database-reviewer | ecc | Team 5 · Database |
| network-config-reviewer | ecc | Team 6 · Networking |
| agentic-rag-math-agent, agentic-typed-rag-pydanticai | awesome-llm | Team 8 · RAG Pipeline |
| gemini-agentic-rag, llama3-1-local-rag, qwen-local-rag | awesome-llm | Team 8 · RAG Pipeline |
| local-hybrid-search-rag, rag-agent-cohere | awesome-llm | Team 8 · RAG Pipeline |
| mobile | roles.yaml | Team 11 · Cross-Platform |
| engineering-iot-fleet-engineer | agency-agents | Team 12 · Development |
| product-behavioral-nudge-engine | agency-agents | Team 15 · Strategy |
| ai-teaching-agent-team | awesome-llm | Team 19 · Training |
| agent-harness-construction | ecc | Team 30 · Build |
| agent-evaluator, harness-optimizer | ecc | Team 30 · Evaluate |
| enterprise-agent-ops | ecc | Team 30 · Scale |
| ai-codebase-migration-agent | awesome-llm | Team 31 · Modernize |
| engineering-minimal-change-engineer | agency-agents | Team 41 · Code |

## সীমা (যা এই ডকুমেন্ট দাবি করে না)

* এটি **roster/architecture ম্যাপ** — runtime routing এখনো role-ভিত্তিক (`roles.yaml` + `roles.d/`);
  team মানে এখানে documentation-স্তরের grouping, `pipelines.yaml`-এর 실행যোগ্য pipeline নয়।
* "Member" = role-এর সংখ্যা, মানুষ নয়; কোনো role-এর বাস্তব দক্ষতা/credentials এর প্রমাণ নয়
  (persona-ই সীমা, ঠিক যেমন `roles.d/security-practitioners.yaml`-এ বলা আছে)।
