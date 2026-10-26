# Security practitioner roles

The security pack adds **37 specialist roles** to the checkout's existing 963-role
registry, for **1,000 roles**. Fifteen requested roles already have equivalent roles;
their definitions are left unchanged. These counts describe the committed packs,
not additional user/workspace overrides.

## Requested-role audit

| # | Requested role | Result / role slug |
|---|---|---|
| 1 | Red Team Operator | Existing: `pentesting` (alias `red-team`); `security-penetration-tester` explicitly includes red-team operations |
| 2 | Web Application Pentester | Added: `web-application-pentester` |
| 3 | API Security Tester | Added: `api-security-tester` |
| 4 | Mobile App Pentester | Added: `mobile-app-pentester` |
| 5 | Wireless Security Auditor | Added: `wireless-security-auditor` |
| 6 | Active Directory Attack Specialist | Added: `active-directory-attack-specialist` |
| 7 | Cloud Pentester (AWS/Azure/GCP) | Added: `cloud-pentester` |
| 8 | Social Engineering Analyst | Added: `social-engineering-analyst` |
| 9 | Physical Security Assessor | Added: `physical-security-assessor` |
| 10 | Bug Bounty Hunter Coach | Existing: `bug-bounty` (scoped research, reproduction, report quality) |
| 11 | SOC Analyst (Tier 1/2/3) | Added: `soc-analyst` |
| 12 | Threat Hunter | Existing: `security-threat-detection-engineer` explicitly identifies as a threat hunter and defines hypothesis-driven hunts |
| 13 | Incident Responder | Existing: `security-incident-responder` |
| 14 | Detection Engineer | Existing: `blue-team`; `security-threat-detection-engineer` |
| 15 | SIEM Engineer | Added: `siem-engineer` |
| 16 | Purple Team Coordinator | Added: `purple-team-coordinator` |
| 17 | Security Hardening Advisor | Added: `security-hardening-advisor` |
| 18 | Zero Trust Architect | Added: `zero-trust-architect` |
| 19 | Deception Technology Specialist | Added: `deception-technology-specialist` |
| 20 | Security Automation (SOAR) Engineer | Added: `soar-engineer` |
| 21 | Digital Forensics Investigator | Existing: `forensics` (evidence handling, chain of custody, disk/memory artifacts and timelines) |
| 22 | Memory Forensics Analyst | Added: `memory-forensics-analyst` |
| 23 | Network Forensics Analyst | Added: `network-forensics-analyst` |
| 24 | Malware Analyst | Existing: `malware-analysis` |
| 25 | Reverse Engineer | Existing: `reverse-engineering` |
| 26 | Mobile Forensics Examiner | Added: `mobile-forensics-examiner` |
| 27 | OSINT Investigator | Existing: `osint` (also `osint-investigation`) |
| 28 | Cyber Threat Intelligence Analyst | Existing: `security-threat-intelligence-analyst` |
| 29 | Dark Web Monitoring Analyst | Added: `dark-web-monitoring-analyst` |
| 30 | Attack Surface Management Specialist | Added: `attack-surface-management-specialist` |

| 31 | Secure Code Reviewer | Existing: `application-security-engineer`; `security-reviewer` |
| 32 | DevSecOps Engineer | Existing: `devsecops` |
| 33 | Container & Kubernetes Security Specialist | Added: `container-kubernetes-security-specialist` |
| 34 | Cryptography Advisor | Existing: `cryptography` |
| 35 | Threat Modeling Expert | Existing: `security-architect`; `application-security-engineer` |
| 36 | Compliance & Risk Analyst | Existing: `compliance`; `security-compliance-auditor` |
| 37 | ICS/SCADA Security Specialist | Added: `ics-scada-security-specialist` |
| 38 | IoT Security Tester | Added: `iot-security-tester` |
| 39 | LLM/AI Red Teamer | Added: `llm-ai-red-teamer` |
| 40 | CTF Mentor | Added: `ctf-mentor` |

| 41 | Coverage-Guided Fuzzing Expert | Added: `coverage-guided-fuzzing-expert` |
| 42 | Vulnerability Root-Cause Researcher | Added: `vulnerability-root-cause-researcher` |
| 43 | Browser Security Researcher | Added: `browser-security-researcher` |
| 44 | OAuth & OIDC Security Assessment Specialist | Added: `oauth-oidc-security-specialist` |
| 45 | SAML Federation Security Assessor | Added: `saml-federation-security-assessor` |
| 46 | Network Protocol Security Researcher | Added: `network-protocol-security-researcher` |
| 47 | Firmware Security Researcher | Added: `firmware-security-researcher` |
| 48 | Software Supply-Chain Security Researcher | Added: `software-supply-chain-security-researcher` |
| 49 | CI/CD Pipeline Penetration Tester | Added: `ci-cd-pipeline-pentester` |
| 50 | SaaS Tenant Isolation Assessor | Added: `saas-tenant-isolation-assessor` |
| 51 | Hardware Root-of-Trust Security Assessor | Added: `hardware-root-of-trust-security-assessor` |
| 52 | Database Security Penetration Tester | Added: `database-security-pentester` |

General pentesting, DFIR, cloud architecture and CTI personas mention some of the
new specialties. A passing mention or adjacent generalist role is not treated as
a dedicated equivalent: e.g. memory-image investigation, SIEM ingestion engineering,
enterprise/device-aware zero trust, or continuous attack-surface management. The
existing threat-detection role, in contrast, explicitly includes the threat-hunter
identity and a complete structured-hunting mission.

Section E was supplied subsequently and audited as roles 31–40. Its five new
personas preserve the supplied prompt text verbatim and reuse the common rules.
`ctf-mentor` is deliberately distinct from `ctf-player`: it offers progressive
hints and withholds the full solution until requested, whereas the player persona
provides the solve path directly. Similarly, general AI safety evaluations,
firmware development, IoT fleet operations, and Kubernetes deployment patterns
are not equivalent to dedicated LLM/agent red teaming, device security testing,
industrial safety-first assessments, or container/cluster security specialization.
Secure code review and threat modeling are already explicit primary missions of
the existing AppSec and security-architect roles, rather than incidental mentions.

## Additional expert research roles (41–52)

The twelve additional roles are distinct specialist research/assessment personas,
not duplicates of generic pentesting, identity administration, firmware development,
IoT testing, or defensive DevSecOps. They cover fuzz-harness construction, patch
root-cause analysis, browser origin boundaries, OAuth/OIDC, SAML federation,
protocol state machines, firmware images, dependency/artifact trust, offensive
pipeline validation, SaaS tenant isolation, hardware roots of trust, and database
privilege boundaries. Each defines technical methodology, lab/test prerequisites,
evidence and uncertainty requirements, remediation, and verification deliverables.
All previous 25 security-pack definitions are preserved unchanged, as are the
963 baseline roles. The latest request adds exactly 12 roles; cumulative additions
are 37, with the original 15 equivalents still skipped.

## Runtime behavior

`roles.d/security-practitioners.yaml` is a normal data-only role layer. All new
roles stay in the existing `security` category, inherit the configured default
agent, and support normal keyword and explicit-role selection. Common rules are
stored once as a YAML anchor, resolved into each role's `instructions`, and
included by `render_system_prompt` alongside the specialization persona.

New-role rules require the scope assumption for offensive work, factual commands
and evidence, uncertainty/verification, no working malware/ransomware or
unauthorized third-party targeting, at most one necessary clarifying question,
and this response order:

**Summary → Findings/Steps → Evidence/Commands → Risk rating → Remediation → References**

Use relevant ATT&CK, OWASP, CIS or NIST references. Unavailable evidence must not
be fabricated. Existing roles intentionally retain their existing instructions;
this pack does not silently rewrite them or change global policy.

A scope assumption is not execution authorization. Role selection grants no new
tools, credentials, network access, permissions, or approval bypass. Personas do
not establish actual professional credentials or prove that a scan occurred.

Wheel and source distributions include `roles.d/`, keeping existing equivalents
and new specializations available together instead of losing imported equivalents
when installing outside the checkout. Restart an already running server to load
new role definitions; then refresh its role picker. No new frontend endpoints or
security-policy changes are needed.
