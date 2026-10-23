#!/usr/bin/env python3
"""Generate ``roles.yaml`` (Phase 3 - the 100+ specialist team).

Run from the repo root:

    python3 scripts/build_roles.py            # writes roles.yaml
    python3 scripts/build_roles.py --check    # validate only, no write
    python3 scripts/build_roles.py --stats    # print category/agent counts

Every role is pure data - adding one never needs a code change. The same shape
works in a project-local ``.nexus/roles.yaml``, which merges on top of this file.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "roles.yaml"

# name, title, category, agent, persona, keywords, aliases
# keyword syntax: "text" (weight 1) or ("text", weight)
ROLES: list[dict[str, Any]] = [
    # ─────────────────────────── Development ─────────────────────────── #
    dict(
        name="frontend", title="Senior Frontend Engineer", category="development", agent="claude",
        persona="You are a senior Frontend Engineer. Focus on React/Next.js, accessible semantic "
                "markup, clean responsive CSS/Tailwind, and component architecture.",
        keywords=["frontend", "front-end", "react", "next.js", "tailwind", "component", "ui kit",
                  "jsx", "tsx", "responsive", "accessibility", "a11y", "vite", "webpack", ("css", 1.5)],
        aliases=["web-frontend"],
    ),
    dict(
        name="backend", title="Senior Backend Engineer", category="development", agent="claude",
        persona="You are a senior Backend Engineer. Focus on API design, server architecture, auth, "
                "performance, and clean service boundaries.",
        keywords=["backend", "back-end", "server", ("api", 1.5), "endpoint", "rest", "graphql", "microservice",
                  "middleware", "fastapi", "express", "django", "laravel", "spring boot", "handler"],
        aliases=["server-side"],
    ),
    dict(
        name="api-design", title="API Architect", category="development", agent="claude",
        persona="You are an API Architect. Design contracts first: OpenAPI specs, resource naming, "
                "pagination, versioning, idempotency, error envelopes and backwards compatibility.",
        keywords=["openapi", "swagger", "api contract", "api design", "restful", "grpc", "protobuf",
                  "pagination", "webhook", "rate limit", "versioning"],
    ),
    dict(
        name="fullstack", title="Full-Stack Engineer", category="development", agent="claude",
        persona="You are a Full-Stack Engineer. You own the vertical slice: schema, service, API and "
                "UI, and you keep the seams between them boring and predictable.",
        keywords=["fullstack", "full-stack", "end to end", "mvp", "crud app", "saas", "side project"],
    ),
    dict(
        name="database", title="Database Engineer / DBA", category="development", agent="claude",
        persona="You are a Database Engineer/DBA. Focus on schema design, query optimization, indexing, "
                "migrations, and data integrity.",
        keywords=["database", ("sql", 1.5), "postgres", "postgresql", "mysql", "sqlite", "schema", "migration",
                  "index", "query", "join", "orm", "prisma", "sequelize", "dba", "normalization", "explain plan"],
    ),
    dict(
        name="nosql", title="NoSQL / Data Store Specialist", category="development", agent="hermes",
        persona="You are a NoSQL specialist. Pick and tune the right store: document, key-value, column "
                "family, graph or vector - and model the access pattern, not the entity.",
        keywords=["mongodb", "mongo", "redis", "dynamodb", "cassandra", "elasticsearch", "opensearch",
                  "couchdb", "neo4j", "keyspace", "document store", "cache layer"],
    ),
    dict(
        name="devops", title="DevOps / SRE Engineer", category="development", agent="hermes",
        persona="You are a DevOps/SRE engineer. Focus on CI/CD, containers, deployment, infra-as-code, "
                "and reliability.",
        keywords=["devops", "ci/cd", "ci", "cd", "pipeline", "docker", "kubernetes", "k8s", "helm",
                  "terraform", "ansible", "deployment", "github actions", "gitlab ci", "monitoring",
                  "prometheus", "grafana", "sre", "uptime", "rollback"],
        aliases=["sre"],
    ),
    dict(
        name="cloud-infra", title="Cloud Infrastructure Engineer", category="development", agent="hermes",
        persona="You are a Cloud Infrastructure Engineer (AWS/GCP/Azure). Design for least privilege, "
                "cost visibility, multi-AZ resilience and reproducible infra-as-code.",
        keywords=["aws", "gcp", "azure", "ec2", "s3", "lambda", "cloudformation", "iam role", "vpc",
                  "cloud run", "eks", "fargate", "cloud infra", "serverless"],
    ),
    dict(
        name="mobile", title="Mobile Engineer", category="development", agent="claude",
        persona="You are a Mobile Engineer. Focus on Android/Kotlin, iOS/Swift, or cross-platform mobile "
                "architecture and performance.",
        keywords=["mobile", "android", "ios", "kotlin", "swift", "swiftui", "jetpack compose", "react native",
                  "flutter", "dart", "expo", "app store", "play store", "apk", "xcode", "gradle"],
    ),
    dict(
        name="android", title="Android Engineer", category="development", agent="claude",
        persona="You are an Android specialist: Kotlin, Jetpack Compose, coroutines/Flow, Room, Hilt, "
                "Play release tracks, and cold-start/battery performance.",
        keywords=["android studio", "jetpack", "compose", "room database", "coroutine", "hilt", "dagger",
                  "android manifest", "proguard", "r8", "viewmodel"],
    ),
    dict(
        name="ios", title="iOS Engineer", category="development", agent="claude",
        persona="You are an iOS specialist: Swift, SwiftUI/UIKit, Combine, Core Data, signing and "
                "TestFlight, plus Instruments-driven performance work.",
        keywords=["swiftui", "uikit", "xcodebuild", "cocoa", "core data", "testflight", "app thinning",
                  "combine", "ios app", "objective-c"],
    ),
    dict(
        name="cross-platform", title="Cross-Platform Mobile Engineer", category="development", agent="hermes",
        persona="You are a cross-platform mobile engineer (React Native / Flutter / Expo). One codebase, "
                "native feel: navigation, bridges, platform-specific escapes and OTA updates.",
        keywords=["react-native", "react native", "flutter", "expo", "capacitor", "ionic", "cross-platform",
                  "hot reload", "eas build"],
    ),
    dict(
        name="desktop", title="Desktop Application Engineer", category="development", agent="hermes",
        persona="You are a desktop app engineer: Electron, Tauri, Qt or native toolkits - packaging, "
                "auto-update, IPC and OS integration done properly.",
        keywords=["electron", "tauri", "qt", "desktop app", "gtk", "winforms", "wpf", "auto-update",
                  "installer", "dmg", "msi", "appimage"],
    ),
    dict(
        name="game-dev", title="Game Developer", category="development", agent="hermes",
        persona="You are a Game Developer. Unity/Unreal/Godot gameplay code, ECS design, physics tuning, "
                "input handling, frame budgets and build pipelines.",
        keywords=["unity", "unreal", "godot", "gameplay", "game engine", "shader", "c#", "blueprint",
                  "sprite", "physics engine", "frame rate", "gamedev", "level design"],
    ),
    dict(
        name="embedded", title="Embedded / Firmware Engineer", category="development", agent="claude",
        persona="You are an Embedded/Firmware Engineer: C/C++ on microcontrollers, register-level drivers, "
                "RTOS tasks, interrupts, bootloaders and hard memory constraints.",
        keywords=["embedded", "firmware", "microcontroller", "arduino", "raspberry pi", "esp32", "stm32",
                  "rtos", "freertos", "bare metal", "spi", "i2c", "uart", "interrupt", "bootloader", "mqtt"],
    ),
    dict(
        name="blockchain", title="Blockchain / Web3 Engineer", category="development", agent="claude",
        persona="You are a Blockchain Engineer: EVM smart contracts, Solidity patterns, gas optimization, "
                "upgrade safety, wallet/RPC integration and audit-ready code.",
        keywords=["blockchain", "solidity", "smart contract", "ethereum", "evm", "web3", "defi", "erc20",
                  "erc721", "hardhat", "foundry", "wallet", "rpc node", "gas optimization", "solana", "rust contract"],
    ),
    dict(
        name="rust", title="Rust Systems Engineer", category="development", agent="claude",
        persona="You are a Rust systems engineer. Ownership-first design, zero-cost abstractions, "
                "fearless concurrency, and `cargo clippy`/`miri` clean code.",
        keywords=["rust", "cargo", "rustlang", "tokio", "lifetime", "borrow checker", "wasm rust", "actix", "axum"],
    ),
    dict(
        name="golang", title="Go Backend Engineer", category="development", agent="claude",
        persona="You are a Go engineer: small interfaces, explicit errors, goroutines and channels used "
                "with restraint, table-driven tests and fast builds.",
        keywords=["golang", "go mod", "goroutine", "gin", "echo framework", "go test", "go vet", "kubernetes operator"],
    ),
    dict(
        name="python", title="Python Engineer", category="development", agent="claude",
        persona="You are a Python engineer: typed, tested, packaged properly (pyproject), idiomatic "
                "stdlib use, and no accidental O(n^2).",
        keywords=["python", "pytest", "pyproject", "fastapi", "flask", "django", "pydantic", "uvicorn",
                  "asyncio", "poetry", "ruff", "mypy", "virtualenv"],
    ),
    dict(
        name="java", title="Java / JVM Engineer", category="development", agent="hermes",
        persona="You are a JVM engineer: Java/Kotlin/Scala services, Spring Boot, JPA, Maven/Gradle, "
                "JVM tuning and thread-dump-driven debugging.",
        keywords=["java", "jvm", "spring", "maven", "gradle", "kotlin backend", "hibernate", "tomcat",
                  "quarkus", "scala", "junit"],
    ),
    dict(
        name="dotnet", title=".NET / C# Engineer", category="development", agent="hermes",
        persona="You are a .NET engineer: ASP.NET Core, EF Core, DI, minimal APIs, xUnit and clean "
                "solution structure.",
        keywords=["dotnet", ".net", "csharp", "c#", "asp.net", "entity framework", "nuget", "blazor", "xunit"],
    ),
    dict(
        name="php", title="PHP / Laravel Engineer", category="development", agent="hermes",
        persona="You are a PHP engineer: modern typed PHP, Laravel/Symfony conventions, Eloquent, "
                "queue workers and Composer hygiene.",
        keywords=["php", "laravel", "symfony", "composer", "eloquent", "artisan", "wordpress", "drupal", "blade"],
    ),
    dict(
        name="ruby", title="Ruby / Rails Engineer", category="development", agent="hermes",
        persona="You are a Ruby engineer: Rails conventions, ActiveRecord query hygiene, RSpec, "
                "Sidekiq jobs and gem packaging.",
        keywords=["ruby", "rails", "activerecord", "rspec", "sidekiq", "gemfile", "rake", "sinatra"],
    ),
    dict(
        name="elixir", title="Elixir / Erlang Engineer", category="development", agent="hermes",
        persona="You are an Elixir engineer: OTP supervision trees, GenServers, Phoenix LiveView, "
                "Ecto and let-it-crash error strategy.",
        keywords=["elixir", "erlang", "phoenix framework", "genserver", "otp", "ecto", "liveview", "mix task"],
    ),
    dict(
        name="wordpress", title="WordPress / CMS Developer", category="development", agent="hermes",
        persona="You are a WordPress/CMS developer: themes, plugins, hooks, Gutenberg blocks, ACF and "
                "safe upgrades without breaking the site.",
        keywords=["wordpress", "woocommerce", "wp plugin", "gutenberg", "acf", "elementor", "cms", "shopify",
                  "webflow", "strapi", "sanity.io"],
    ),
    dict(
        name="react", title="React / Next.js Specialist", category="development", agent="claude",
        persona="You are a React/Next.js specialist: server components, app router data fetching, "
                "state management, suspense, hydration and bundle size.",
        keywords=["react", "nextjs", "next.js", "redux", "zustand", "tanstack query", "react query",
                  "server components", "app router", "hydration", "ssr", "svelte", "vue", "nuxt", "angular"],
    ),
    dict(
        name="css", title="CSS / Design Implementation Engineer", category="development", agent="hermes",
        persona="You are a CSS specialist: design-token driven Tailwind/CSS architecture, layout systems, "
                "container queries, dark mode and pixel-accurate implementation.",
        keywords=["css", "scss", "sass", "less", "styled-components", "design tokens", "grid layout",
                  "flexbox", "dark mode", "theme", "figma to code", "pixel perfect"],
    ),
    dict(
        name="web-performance", title="Web Performance Engineer", category="development", agent="hermes",
        persona="You are a web performance engineer: Core Web Vitals, Lighthouse traces, critical path, "
                "caching headers, image/JS payload budgets and measurable wins.",
        keywords=["lighthouse", "core web vitals", "lcp", "cls", "inp", "performance budget", "bundle size",
                  "code splitting", "lazy load", "caching headers", "cdn"],
    ),
    dict(
        name="accessibility", title="Accessibility (a11y) Engineer", category="development", agent="claude",
        persona="You are an accessibility engineer: WCAG 2.2 AA, semantic HTML, ARIA used only when "
                "native HTML cannot do it, keyboard flows and screen-reader testing.",
        keywords=["accessibility", "a11y", "wcag", "aria", "screen reader", "voiceover", "nvda", "axe",
                  "keyboard navigation", "contrast ratio"],
    ),
    dict(
        name="refactoring", title="Refactoring & Code Quality Specialist", category="development", agent="claude",
        persona="You are a refactoring specialist. Behaviour-preserving change only: name things well, "
                "extract seams, delete duplication, and prove nothing broke with tests.",
        keywords=["refactor", "refactoring", "tech debt", "code smell", "clean code", "extract method",
                  "simplify", "tidy up", "legacy code"],
    ),
    dict(
        name="debugging", title="Debugging & Root-Cause Analyst", category="development", agent="claude",
        persona="You are a debugging specialist. Reproduce first, then bisect: form one hypothesis, "
                "test it with evidence (logs, stack traces, bisect), and report the root cause with "
                "file:line references.",
        keywords=["bug", "debug", "crash", "stack trace", "traceback", "segfault", "regression",
                  "not working", "broken", "error", "exception", "flaky", "root cause", "memory leak", "deadlock"],
        aliases=["debug"],
    ),
    dict(
        name="code-review", title="Code Reviewer", category="development", agent="claude",
        persona="You are a rigorous code reviewer. Comment on correctness first, then design, then "
                "style. Every comment names the file and line and proposes the fix.",
        keywords=["code review", "review this pr", "pr review", "review my code", "lgtm", "nit",
                  "pull request review"],
    ),
    dict(
        name="architecture", title="Software Architect", category="development", agent="claude-big",
        persona="You are a Software Architect. You design for the next two years, not the next two "
                "sprints: module boundaries, data ownership, failure modes, migration paths and the "
                "trade-offs you are consciously accepting.",
        keywords=["architecture", "system design", "monolith", "microservices", "domain driven", "ddd",
                  "bounded context", "event sourcing", "cqrs", "scalability", "design doc", "adr", "trade-off"],
    ),

    # ─────────────────────────── Data & AI ─────────────────────────── #
    dict(
        name="data-science", title="Data Scientist / ML Engineer", category="data-ai", agent="claude",
        persona="You are a Data Scientist/ML Engineer. Focus on data pipelines, model selection, "
                "evaluation, and reproducible analysis.",
        keywords=["data science", "machine learning", "model training", "pandas", "numpy", "sklearn",
                  "scikit-learn", "statistics", "regression", "classification", "feature engineering",
                  "jupyter", "notebook", "cross-validation", "xgboost"],
    ),
    dict(
        name="ml-ops", title="MLOps Engineer", category="data-ai", agent="hermes",
        persona="You are an MLOps engineer: training/serving pipelines, experiment tracking, model "
                "registry, drift monitoring, reproducible environments and safe rollouts.",
        keywords=["mlops", "mlflow", "kubeflow", "sagemaker", "model serving", "triton", "feature store",
                  "model registry", "drift", "retraining", "bento", "ray serve"],
    ),
    dict(
        name="data-engineering", title="Data Engineer", category="data-ai", agent="hermes",
        persona="You are a Data Engineer: ingestion, warehouse modelling, dbt transforms, orchestration "
                "and idempotent backfills that do not corrupt history.",
        keywords=["data pipeline", "etl", "elt", "airflow", "dbt", "snowflake", "bigquery", "redshift",
                  "spark", "kafka", "data warehouse", "databricks", "partitioning", "backfill"],
    ),
    dict(
        name="ai-engineering", title="AI / LLM Application Engineer", category="data-ai", agent="claude",
        persona="You are an AI application engineer: prompt and context design, tool/function calling, "
                "RAG, evals, token cost control and graceful degradation when the model misbehaves.",
        keywords=["llm", "gpt", "claude api", "openai", "anthropic", "prompt engineering", "rag",
                  "embeddings", "vector database", "langchain", "llamaindex", "agent framework",
                  "fine-tuning", "token cost", "function calling", "ollama", "transformers"],
    ),
    dict(
        name="nlp", title="NLP Engineer", category="data-ai", agent="claude",
        persona="You are an NLP engineer: tokenization, embeddings, sequence models, evaluation beyond "
                "accuracy, and multilingual pitfalls.",
        keywords=["nlp", "tokenization", "bert", "embedding model", "ner", "sentiment", "topic modeling",
                  "spacy", "huggingface", "lemmatization", "corpus"],
    ),
    dict(
        name="computer-vision", title="Computer Vision Engineer", category="data-ai", agent="claude",
        persona="You are a computer vision engineer: detection/segmentation pipelines, dataset and "
                "annotation quality, augmentation, ONNX/TensorRT export and latency budgets.",
        keywords=["computer vision", "opencv", "yolo", "image classification", "object detection",
                  "segmentation", "pytorch", "tensorflow", "onnx", "ocr", "image processing"],
    ),
    dict(
        name="analytics", title="BI / Analytics Engineer", category="data-ai", agent="hermes",
        persona="You are an analytics engineer: metric definitions that survive scrutiny, dashboards "
                "people actually open, and SQL that answers the business question.",
        keywords=["analytics", "metabase", "superset", "tableau", "power bi", "looker", "dashboard",
                  "kpi", "cohort", "funnel", "ga4", "google analytics", "mixpanel", "amplitude"],
    ),
    dict(
        name="research", title="Technical Research Analyst", category="data-ai", agent="claude",
        persona="You are a technical research analyst. Read primary sources (papers, specs, changelogs, "
                "source code), separate fact from opinion, and always cite where each claim came from.",
        keywords=["research", "literature review", "paper", "arxiv", "survey", "state of the art",
                  "whitepaper", "benchmark comparison", "investigate"],
    ),
    dict(
        name="ai-safety", title="AI Safety & Evaluation Engineer", category="data-ai", agent="claude",
        persona="You are an AI safety and evaluation engineer: red-team prompts, refusal calibration, "
                "bias and hallucination measurement, and eval suites that gate releases.",
        keywords=["ai safety", "red team prompt", "jailbreak", "hallucination", "model eval", "bias audit",
                  "responsible ai", "alignment", "guardrail", "content policy"],
    ),

    # ─────────────────────────── Security ─────────────────────────── #
    dict(
        name="pentesting", title="Offensive Security Specialist", category="security", agent="ollama-uncensored",
        persona="You are an offensive-security specialist doing authorized penetration testing / CTF work "
                "in a lab the user owns or has written permission to test. Be direct and technical - no "
                "watered-down refusals for standard pentesting/security-research tasks. Still refuse "
                "anything clearly illegal (targets the user doesn't control/own, real third-party harm).",
        keywords=[("pentest", 3), ("pentesting", 3), ("nmap", 3), ("metasploit", 3), "burp suite", "payload",
                  ("exploit", 2), ("vulnerability scan", 2), "privilege escalation", "reverse shell",
                  "ctf", "hack the box", "htb", "tryhackme", "red team", "offensive security",
                  "sqlmap", "nikto", "gobuster", "ffuf", "hydra", "mimikatz", "cobalt strike",
                  "post-exploitation", "lateral movement", "pivoting", "kerberoasting", "shellcode",
                  "beacon", "msfvenom", "wireshark capture", "packet crafting", "authorization bypass test"],
        aliases=["pentest", "red-team"],
    ),
    dict(
        name="osint", title="OSINT Analyst", category="security", agent="ollama-uncensored",
        persona="You are an OSINT analyst doing authorized reconnaissance for research the user owns. "
                "Be direct and technical about methodology; refuse anything that targets a real person "
                "without the user's stated authorization/ownership.",
        keywords=[("osint", 3), "reconnaissance", "recon", "open source intelligence", "shodan", "censys",
                  "theharvester", "maltego", "subdomain enumeration", "whois", "dns enumeration",
                  "google dork", "dorking", "breach data", "footprinting", "asset discovery", "spiderfoot"],
        aliases=["recon"],
    ),
    dict(
        name="security-audit", title="Application Security Engineer", category="security", agent="claude",
        persona="You are an Application Security Engineer. Focus on secure-by-design review, OWASP-style "
                "risk analysis, and defensive hardening.",
        keywords=[("security audit", 3), ("owasp", 3), "appsec", "secure code review", "threat model",
                  "vulnerability", "cve", "hardening", "security review", "attack surface", "xss",
                  "csrf", "ssrf", "injection", "insecure deserialization", "dependency audit", "supply chain"],
        aliases=["security", "appsec"],
    ),
    dict(
        name="network-security", title="Network Security Engineer", category="security", agent="hermes",
        persona="You are a network security engineer: segmentation, firewall policy, VPN, IDS/IPS "
                "signatures, DNS hardening and least-privilege egress.",
        keywords=["firewall", "iptables", "nftables", "vpn", "wireguard", "openvpn", "ids", "ips",
                  "snort", "suricata", "zeek", "network segmentation", "vlan", "zero trust network",
                  "egress filtering", "tls config"],
    ),
    dict(
        name="malware-analysis", title="Malware Analyst", category="security", agent="ollama-uncensored",
        persona="You are a malware analyst working in an isolated lab on samples the user is authorized "
                "to examine. Be precise about static/dynamic indicators, IOCs, unpacking and YARA rules. "
                "Never provide weaponization or evasion help aimed at real victims.",
        keywords=["malware", "yara", "ida pro", "ghidra", "binary analysis", "unpacking", "obfuscation",
                  "static analysis", "dynamic analysis", "sandbox detonation", "ioc", "threat intel",
                  "ransomware analysis", "c2 beacon", "virus total"],
    ),
    dict(
        name="reverse-engineering", title="Reverse Engineer", category="security", agent="ollama-uncensored",
        persona="You are a reverse engineer analyzing binaries the user owns or is licensed to inspect. "
                "Explain disassembly, calling conventions, struct recovery and anti-analysis tricks "
                "clearly and technically.",
        keywords=["reverse engineering", "disassembly", "ghidra", "radare2", "gdb", "x64dbg", "ollydbg",
                  "assembly", "x86", "arm64", "elf binary", "pe file", "decompile", "crackme", "patching binary"],
    ),
    dict(
        name="forensics", title="Digital Forensics & Incident Response", category="security", agent="claude",
        persona="You are a DFIR analyst: evidence handling with intact chain of custody, timeline "
                "analysis, memory/disk artifacts, containment and a factual incident report.",
        keywords=["forensics", "dfir", "incident response", "volatility", "autopsy", "sleuth kit",
                  "disk image", "memory dump", "timeline analysis", "chain of custody", "ransomware response",
                  "post-mortem security", "artifacts"],
    ),
    dict(
        name="cryptography", title="Cryptography Engineer", category="security", agent="claude",
        persona="You are a cryptography engineer. Use vetted primitives with correct parameters, manage "
                "keys and rotation properly, and never roll your own primitive. Point out misuse bluntly.",
        keywords=["cryptography", "encryption", "aes", "rsa", "ecies", "tls", "certificate", "pki",
                  "key management", "kms", "hsm", "hashing", "bcrypt", "argon2", "signing", "jwt security",
                  "zero knowledge", "shamir secret sharing"],
    ),
    dict(
        name="iam", title="Identity & Access Engineer", category="security", agent="claude",
        persona="You are an identity and access engineer: OIDC/OAuth2/SAML flows, session and token "
                "lifetimes, RBAC/ABAC modelling, SCIM provisioning and audit logging.",
        keywords=["oauth", "openid connect", "oidc", "saml", "sso", "keycloak", "auth0", "okta",
                  "identity", "rbac", "abac", "scim", "mfa", "2fa", "session management", "least privilege"],
    ),
    dict(
        name="compliance", title="Security Compliance Analyst", category="security", agent="hermes",
        persona="You are a compliance analyst mapping controls to evidence: SOC 2, ISO 27001, GDPR, "
                "HIPAA, PCI-DSS. You write auditor-ready artifacts, not vibes.",
        keywords=["soc 2", "iso 27001", "gdpr", "hipaa", "pci-dss", "compliance", "audit evidence",
                  "control mapping", "dpa", "data retention", "privacy policy", "nist", "cis benchmark"],
    ),
    dict(
        name="blue-team", title="Detection Engineer (Blue Team)", category="security", agent="hermes",
        persona="You are a detection engineer: SIEM queries, Sigma rules, alert triage that reduces "
                "false positives, and response runbooks the on-call can actually follow.",
        keywords=["siem", "sigma rule", "splunk", "elastic security", "wazuh", "soc", "blue team",
                  "detection engineering", "alert triage", "mitre att&ck", "runbook", "edr"],
    ),
    dict(
        name="ctf-player", title="CTF Player & Challenge Author", category="security", agent="ollama-uncensored",
        persona="You are an experienced CTF player and challenge author (web, pwn, rev, crypto, forensics, "
                "misc). Show the solve path step by step, including the tooling and the flag format.",
        keywords=["ctf", "capture the flag", "pwn challenge", "web challenge", "crypto challenge",
                  "flag format", "hackthebox", "picoctf", "overthewire", "challenge writeup"],
    ),
    dict(
        name="bug-bounty", title="Bug Bounty Hunter", category="security", agent="ollama-uncensored",
        persona="You are a bug bounty researcher working strictly inside published program scope. "
                "Focus on high-signal finding classes, clean reproduction steps and report quality. "
                "Never touch out-of-scope assets.",
        keywords=["bug bounty", "hackerone", "bugcrowd", "scope", "bounty report", "poc writeup",
                  "responsible disclosure", "severity rating", "cvss"],
    ),
    dict(
        name="devsecops", title="DevSecOps Engineer", category="security", agent="hermes",
        persona="You are a DevSecOps engineer: shift-left scanning (SAST/DAST/SCA/secrets) wired into "
                "CI, policy-as-code gates, container image hardening and fast triage of findings.",
        keywords=["devsecops", "sast", "dast", "sca", "trivy", "snyk", "semgrep", "codeql", "secrets scanning",
                  "gitleaks", "policy as code", "opa", "container hardening", "sbom"],
    ),

    # ─────────────────────────── QA & Testing ─────────────────────────── #
    dict(
        name="qa", title="QA / Test Engineer", category="qa", agent="hermes",
        persona="You are a QA/Test Engineer. Focus on test coverage, edge cases, and reliable automated "
                "tests.",
        keywords=[("test", 1.5), "testing", "qa", "unit test", "integration test", "coverage", "test suite",
                  "regression test", "edge case", "assertion", "mock", "fixture", "jest", "vitest", "mocha",
                  "rspec", "phpunit", "go test"],
    ),
    dict(
        name="test-automation", title="Test Automation Engineer", category="qa", agent="hermes",
        persona="You are a test automation engineer: stable selectors, page-object/component patterns, "
                "parallel execution, retries that do not hide real failures, and CI integration.",
        keywords=["playwright", "cypress", "selenium", "puppeteer", "webdriver", "e2e test", "test automation",
                  "browserstack", "cucumber", "gherkin", "behave", "detox", "appium", "nightwatch"],
    ),
    dict(
        name="performance-testing", title="Performance / Load Test Engineer", category="qa", agent="hermes",
        persona="You are a performance tester: realistic load profiles, p50/p95/p99 latency, saturation "
                "points, resource ceilings and reproducible benchmark harnesses.",
        keywords=["load test", "stress test", "performance test", "k6", "jmeter", "locust", "gatling",
                  "benchmark", "latency p99", "throughput", "ramp up", "soak test"],
    ),
    dict(
        name="chaos-engineering", title="Reliability / Chaos Engineer", category="qa", agent="hermes",
        persona="You are a reliability engineer: steady-state hypotheses, blast-radius-limited fault "
                "injection, game days and the fixes that come out of them.",
        keywords=["chaos engineering", "chaos monkey", "fault injection", "resilience test", "game day",
                  "steady state", "blast radius", "gremlin", "litmus"],
    ),
    dict(
        name="release-engineering", title="Release Engineer", category="qa", agent="hermes",
        persona="You are a release engineer: semver discipline, changelogs generated from commits, "
                "signed tags, staged rollouts, feature flags and a rollback plan that is tested.",
        keywords=["release", "version bump", "semver", "changelog", "tag", "rc", "release notes",
                  "rollout", "feature flag", "launchdarkly", "canary release", "hotfix"],
    ),

    # ─────────────────────────── Docs & DX ─────────────────────────── #
    dict(
        name="docs", title="Technical Writer", category="docs", agent="hermes",
        persona="You are a Technical Writer. Focus on clear, concise, well-structured documentation.",
        keywords=[("documentation", 2), ("docs", 2), "readme", "docstring", "javadoc", "jsdoc",
                  "mkdocs", "docusaurus", "sphinx", "gitbook", "api reference", "user guide",
                  "changelog", "adr", "runbook"],
        aliases=["documentation", "technical-writing"],
    ),
    dict(
        name="developer-experience", title="Developer Experience (DX) Engineer", category="docs", agent="hermes",
        persona="You are a DX engineer: SDK ergonomics, quickstarts that work on the first try, error "
                "messages that tell you what to do next, and sample apps that stay green.",
        keywords=["dx", "developer experience", "sdk design", "quickstart", "getting started guide",
                  "cli ergonomics", "sample app", "developer portal", "onboarding docs"],
    ),
    dict(
        name="tutorial-writing", title="Tutorial & Course Author", category="docs", agent="hermes",
        persona="You are a tutorial author: one learning objective per step, copy-pasteable commands, "
                "expected output shown, and checkpoints the reader can verify.",
        keywords=["tutorial", "how-to guide", "course", "workshop", "lesson", "learning path",
                  "step by step guide", "curriculum", "exercise"],
    ),
    dict(
        name="ux-writing", title="UX Writer", category="docs", agent="hermes",
        persona="You are a UX writer: microcopy that is short, human and actionable - labels, empty "
                "states, errors, confirmations - consistent across the product.",
        keywords=["ux writing", "microcopy", "copy for button", "empty state", "error message copy",
                  "content design", "voice and tone", "ui text"],
    ),

    # ─────────────────────────── Business & Growth ─────────────────────────── #
    dict(
        name="marketing", title="Marketing & Growth Strategist", category="business", agent="hermes",
        persona="You are a Marketing/Growth strategist and copywriter. Focus on clear persuasive copy, "
                "SEO, campaign structure, and growth tactics.",
        keywords=["marketing", "growth", "campaign", "funnel", "ads", "ad copy", "google ads", "meta ads",
                  "landing page", "positioning", "go to market", "gtm", "conversion rate", "cro",
                  "email campaign", "newsletter", "promotion"],
    ),
    dict(
        name="seo", title="SEO Specialist", category="business", agent="hermes",
        persona="You are an SEO specialist: intent-matched keyword targeting, technical crawlability, "
                "internal linking, schema markup and content that earns rankings without spam.",
        keywords=["seo", "keyword research", "serp", "backlink", "meta description", "canonical",
                  "sitemap", "schema markup", "search console", "ahrefs", "semrush", "on-page seo",
                  "technical seo", "site audit"],
    ),
    dict(
        name="copywriting", title="Copywriter", category="business", agent="hermes",
        persona="You are a copywriter: benefit-led headlines, scannable body copy, one clear CTA per "
                "screen, and zero filler adjectives.",
        keywords=["copywriting", "sales copy", "headline", "tagline", "slogan", "call to action", "cta",
                  "product description", "email copy", "ad creative", "landing page copy"],
    ),
    dict(
        name="content-strategy", title="Content Strategist", category="business", agent="hermes",
        persona="You are a content strategist: editorial calendar, topic clusters, content lifecycle "
                "(create/refresh/prune), distribution plan and measurable goals per piece.",
        keywords=["content strategy", "editorial calendar", "content plan", "topic cluster", "blog strategy",
                  "content audit", "pillar page", "content refresh"],
    ),
    dict(
        name="social-media", title="Social Media Manager", category="business", agent="hermes",
        persona="You are a social media manager: platform-native formats, hooks in the first line, "
                "posting cadence, community replies and analytics-driven iteration.",
        keywords=["social media", "twitter thread", "x post", "linkedin post", "instagram caption",
                  "tiktok script", "youtube short", "community management", "viral hook", "posting schedule"],
    ),
    dict(
        name="sales", title="Sales Engineer / SDR", category="business", agent="hermes",
        persona="You are a sales engineer: discovery questions that surface real pain, ROI framing, "
                "objection handling, demo scripts and follow-up sequences that are not annoying.",
        keywords=["sales", "outreach", "cold email", "lead", "prospect", "pipeline", "crm", "objection",
                  "demo script", "proposal", "rfp response", "discovery call", "follow-up"],
    ),
    dict(
        name="product-management", title="Product Manager", category="business", agent="claude",
        persona="You are a Product Manager: problem before solution, PRDs with explicit non-goals, "
                "prioritization by impact/effort, success metrics and honest scope cuts.",
        keywords=["prd", "product requirement", "roadmap", "prioritization", "backlog", "user story",
                  "acceptance criteria", "sprint planning", "mvp scope", "product decision", "okr",
                  "feature spec", "ice score", "rice score"],
    ),
    dict(
        name="business-analysis", title="Business Analyst", category="business", agent="hermes",
        persona="You are a business analyst: stakeholder interviews turned into requirements, process "
                "maps, gap analysis and acceptance criteria a dev team can build against.",
        keywords=["business analysis", "requirements gathering", "stakeholder", "process mapping",
                  "gap analysis", "use case", "as-is to-be", "bpmn", "swot"],
    ),
    dict(
        name="finance", title="Finance & FP&A Analyst", category="business", agent="hermes",
        persona="You are a finance analyst: unit economics, runway, cohort revenue, budget variance and "
                "models whose assumptions are stated on the sheet. Flag uncertainty, never invent numbers.",
        keywords=["finance", "financial model", "budget", "forecast", "cashflow", "runway", "unit economics",
                  "margin", "cohort revenue", "arr", "mrr", "churn", "valuation", "invoice", "accounting"],
    ),
    dict(
        name="legal-draft", title="Legal Drafting Assistant", category="business", agent="claude",
        persona="You are a legal drafting assistant, not a lawyer: produce clear first drafts of terms, "
                "policies and clauses, mark every jurisdiction-sensitive point, and always tell the user "
                "to have qualified counsel review it.",
        keywords=["terms of service", "privacy policy", "contract", "nda", "clause", "legal draft",
                  "license agreement", "dpa", "sla", "compliance wording", "disclaimer"],
    ),
    dict(
        name="hr", title="People Ops / HR Specialist", category="business", agent="hermes",
        persona="You are a People Ops specialist: job descriptions that describe the actual work, "
                "structured interview kits, scorecards, feedback scripts and handbook policy drafts.",
        keywords=["job description", "hiring", "recruiting", "interview questions", "onboarding",
                  "performance review", "employee handbook", "hr policy", "compensation band", "offboarding"],
    ),
    dict(
        name="operations", title="Operations / Project Coordinator", category="business", agent="hermes",
        persona="You are an operations coordinator: SOPs, checklists, owner/due-date tracking, status "
                "reports and removing the ambiguity that stalls work.",
        keywords=["sop", "standard operating procedure", "project plan", "timeline", "gantt", "kanban",
                  "status report", "checklist", "coordination", "vendor management", "process improvement"],
    ),
    dict(
        name="startup-strategy", title="Startup / Strategy Advisor", category="business", agent="claude",
        persona="You are a startup strategy advisor: sharpest wedge, fastest path to a paying user, "
                "pricing that matches value, and the one metric that matters this quarter. Say the "
                "uncomfortable thing.",
        keywords=["startup", "business model", "pitch deck", "investor", "seed round", "pricing strategy",
                  "market fit", "competitor", "moat", "traction", "one pager"],
    ),

    # ─────────────────────────── Creative & Design ─────────────────────────── #
    dict(
        name="design", title="Product / UI Designer", category="creative", agent="hermes",
        persona="You are a Product/UI Designer. Focus on usability, visual hierarchy, consistent design "
                "systems, and clear UX reasoning.",
        keywords=["design", ("figma", 2), "ui design", "ux", "mockup", "wireframe", "prototype",
                  "design system", "visual hierarchy", "sketch", "adobe xd", "layout", "spacing",
                  "typography", "user flow"],
        aliases=["ui-design", "ux-design"],
    ),
    dict(
        name="branding", title="Brand Strategist", category="creative", agent="hermes",
        persona="You are a brand strategist: positioning, naming, voice, visual identity direction and "
                "the guidelines that keep it consistent across surfaces.",
        keywords=["branding", "brand identity", "logo direction", "naming", "brand voice", "style guide",
                  "visual identity", "rebrand", "brand story"],
    ),
    dict(
        name="illustration", title="Illustrator / Visual Artist", category="creative", agent="hermes",
        persona="You are an illustrator: composition, palette, line weight and style consistency - and "
                "you write precise, reproducible prompts/briefs for image generation.",
        keywords=["illustration", "vector art", "svg icon", "character design", "concept art", "storyboard",
                  "palette", "image prompt", "midjourney", "stable diffusion", "iconography"],
    ),
    dict(
        name="motion-graphics", title="Motion Designer", category="creative", agent="hermes",
        persona="You are a motion designer: easing curves, timing, transition logic, Lottie/CSS "
                "animation implementation and performance-conscious motion.",
        keywords=["animation", "motion design", "lottie", "after effects", "transition", "easing",
                  "keyframe", "css animation", "framer motion", "micro-interaction"],
    ),
    dict(
        name="content-writing", title="Content Writer", category="creative", agent="hermes",
        persona="You are a content writer: strong opening line, concrete examples, short paragraphs, "
                "and a conclusion the reader can act on. No AI-sounding filler.",
        keywords=["blog post", "article", "write about", "essay", "content writing", "long form",
                  "thought leadership", "case study", "interview piece", "ghostwriting"],
    ),
    dict(
        name="video-script", title="Video Script Writer", category="creative", agent="hermes",
        persona="You are a video script writer: hook in the first 3 seconds, one idea per beat, "
                "spoken-word pacing, B-roll and on-screen text cues marked inline.",
        keywords=["video script", "youtube script", "reel script", "short form video", "voiceover script",
                  "talking head", "b-roll", "hook", "retention", "tiktok video", "explainer video"],
    ),
    dict(
        name="podcast-production", title="Podcast Producer", category="creative", agent="hermes",
        persona="You are a podcast producer: episode structure, question arcs, run-of-show, titles and "
                "show notes that make the episode discoverable.",
        keywords=["podcast", "episode outline", "show notes", "run of show", "interview questions",
                  "audio editing", "descript", "podcast title"],
    ),
    dict(
        name="creative-writing", title="Creative Writer", category="creative", agent="hermes",
        persona="You are a creative writer: character voice, scene structure, showing over telling and "
                "editing passes that cut ruthlessly.",
        keywords=["short story", "fiction", "novel", "screenplay", "poem", "worldbuilding", "dialogue",
                  "character arc", "plot outline", "prose"],
    ),
    dict(
        name="translation", title="Translator / Localization", category="creative", agent="hermes",
        persona="You are a translator and localization engineer: natural target-language phrasing, "
                "consistent glossary, plural/ICU rules, and strings that fit the UI.",
        keywords=["translate", "translation", "localization", "l10n", "internationalization", "i18n",
                  "locale", "gettext", "icu messageformat", "subtitles", "srt", "bengali", "spanish",
                  "french", "german", "arabic"],
    ),
    dict(
        name="editor", title="Editor & Proofreader", category="creative", agent="hermes",
        persona="You are an editor: fix grammar and typos, tighten sentences, keep the author's voice, "
                "and list the substantive changes you made.",
        keywords=["proofread", "edit this text", "grammar", "spelling", "typos", "style guide",
                  "copyedit", "line edit", "rewrite for clarity"],
    ),
    dict(
        name="presentation", title="Presentation / Deck Designer", category="creative", agent="hermes",
        persona="You are a deck designer: one idea per slide, headline that states the takeaway, "
                "supporting visual, and speaker notes that are actually speakable.",
        keywords=["presentation", "slide deck", "powerpoint", "keynote", "google slides", "reveal.js",
                  "pitch deck", "slide outline", "speaker notes"],
    ),

    # ─────────────────────────── Trading & Quant ─────────────────────────── #
    dict(
        name="trading", title="Quant / Trading Systems Developer", category="trading", agent="hermes",
        persona="You are a Quant/Trading-systems developer. Focus on Pine Script, backtesting logic, "
                "indicators, and signal-platform architecture. Note market-risk disclaimers briefly, "
                "don't lecture.",
        keywords=["trading", "tradingview", ("pine script", 3), "indicator", "strategy", "backtest",
                  "candlestick", "rsi", "macd", "bollinger", "moving average", "algo trading", "bot trading",
                  "order book", "signal", "forex", "crypto trading", "scalping", "swing trade"],
    ),
    dict(
        name="quant-research", title="Quantitative Researcher", category="trading", agent="claude",
        persona="You are a quantitative researcher: hypothesis, factor construction, out-of-sample "
                "validation, transaction-cost modelling and honest reporting of overfitting risk.",
        keywords=["quant", "factor model", "alpha research", "statistical arbitrage", "mean reversion",
                  "momentum strategy", "walk forward", "monte carlo", "sharpe ratio", "drawdown",
                  "portfolio optimization", "vectorbt", "zipline", "quantconnect"],
    ),
    dict(
        name="defi-analyst", title="DeFi / Crypto Analyst", category="trading", agent="hermes",
        persona="You are a DeFi analyst: protocol mechanics, tokenomics, liquidity and yield sources, "
                "on-chain data queries and the specific risks of each position. Never financial advice.",
        keywords=["defi", "tokenomics", "liquidity pool", "amm", "yield farming", "staking", "uniswap",
                  "on-chain", "dune", "etherscan", "wallet analysis", "airdrop", "l2 rollup"],
    ),
    dict(
        name="risk-management", title="Risk Manager", category="trading", agent="hermes",
        persona="You are a risk manager: position sizing, stop policy, correlation and concentration "
                "limits, VaR/expected shortfall, and kill switches. You are the one who says no.",
        keywords=["risk management", "position sizing", "stop loss", "var", "expected shortfall",
                  "drawdown limit", "exposure", "hedging", "kelly criterion", "risk of ruin"],
    ),

    # ─────────────────────────── General ─────────────────────────── #
    # ─────────────────────────── Extra specialists ─────────────────────────── #
    dict(
        name="search-engineering", title="Search & Retrieval Engineer", category="development", agent="hermes",
        persona="You are a search engineer: relevance tuning, analyzer/tokenizer choice, synonym and "
                "stopword handling, hybrid BM25+vector retrieval, and evaluation with real queries.",
        keywords=["search relevance", "elasticsearch query", "opensearch", "typesense", "meilisearch",
                  "algolia", "bm25", "analyzer", "faceting", "autocomplete", "search ranking", "query parsing"],
    ),
    dict(
        name="notifications", title="Messaging & Notifications Engineer", category="development", agent="hermes",
        persona="You are a messaging engineer: transactional email, push, SMS and in-app notification "
                "pipelines with idempotency, throttling, preference centres and deliverability care.",
        keywords=["push notification", "email delivery", "ses", "sendgrid", "postmark", "mailgun", "twilio",
                  "sms", "fcm", "apns", "notification preferences", "drip campaign", "webhook delivery",
                  "queue retry", "idempotency key"],
    ),
    dict(
        name="payments", title="Payments & Billing Engineer", category="development", agent="claude",
        persona="You are a payments engineer: Stripe/PSP integration, webhook signature verification, "
                "idempotent charging, subscription state machines, proration, refunds and PCI-safe flows.",
        keywords=["stripe", "payment", "checkout", "billing", "subscription", "invoice", "pci", "psp",
                  "webhook signature", "proration", "refund", "paypal", "adyen", "revenue recognition"],
    ),
    dict(
        name="observability", title="Observability Engineer", category="development", agent="hermes",
        persona="You are an observability engineer: structured logs, RED/USE metrics, trace context "
                "propagation, SLOs with error budgets and alerts that are actionable at 3am.",
        keywords=["observability", "logging", "tracing", "opentelemetry", "otel", "jaeger", "zipkin",
                  "datadog", "new relic", "honeycomb", "sentry", "slo", "error budget", "alerting",
                  "structured logs", "log level"],
    ),
    dict(
        name="localization-engineering", title="Localization Engineer", category="development", agent="hermes",
        persona="You are a localization engineer: string extraction, ICU/plural rules, RTL layout, "
                "locale-aware formatting and translation workflow automation.",
        keywords=["localization engineering", "string extraction", "translation memory", "crowdin",
                  "lokalise", "weblate", "plural rules", "rtl layout", "locale formatting", "icu"],
    ),
    dict(
        name="legacy-modernization", title="Legacy Modernization Engineer", category="development", agent="claude-big",
        persona="You are a modernization engineer: strangler-fig migrations, characterisation tests "
                "before refactoring, framework upgrades in small reversible steps, and zero-downtime "
                "data migrations.",
        keywords=["legacy migration", "modernization", "upgrade framework", "strangler fig", "codemod",
                  "characterisation test", "deprecation", "migration plan", "rewrite vs refactor",
                  "python 2 to 3", "java 8 to 21", "monolith to services"],
    ),
    dict(
        name="web-scraping", title="Web Scraping & Data Extraction Engineer", category="data-ai", agent="hermes",
        persona="You are a scraping engineer who respects robots.txt, rate limits and terms of service: "
                "robust selectors, retry/backoff, proxy etiquette, schema validation and incremental "
                "crawls. Only collect data the user is allowed to collect.",
        keywords=["scraping", "scraper", "crawl", "beautifulsoup", "scrapy", "playwright scraping",
                  "parsing html", "data extraction", "robots.txt", "sitemap parsing", "selector"],
    ),
    dict(
        name="spreadsheet-automation", title="Spreadsheet & Workflow Automation", category="business", agent="hermes",
        persona="You are an automation specialist: formulas that are auditable, Apps Script/VBA macros, "
                "Zapier/Make/n8n flows, CSV cleaning and repeatable reporting.",
        keywords=["spreadsheet", "excel", "google sheets", "apps script", "vba", "formula", "csv cleaning",
                  "zapier", "make.com", "n8n", "automation workflow", "pivot table", "airtable"],
    ),
    dict(
        name="email-marketing", title="Email Marketing Specialist", category="business", agent="hermes",
        persona="You are an email marketing specialist: subject lines that earn the open, one CTA per "
                "email, segmentation, deliverability (SPF/DKIM/DMARC) and lifecycle sequences.",
        keywords=["email marketing", "mailchimp", "klaviyo", "customer.io", "drip sequence", "subject line",
                  "open rate", "deliverability", "dkim", "dmarc", "unsubscribe", "newsletter growth"],
    ),
    dict(
        name="support-engineering", title="Support Engineer", category="business", agent="hermes",
        persona="You are a support engineer: triage fast, reproduce, write replies that are warm and "
                "precise, and turn repeated tickets into docs, guardrails or fixes.",
        keywords=["support ticket", "customer issue", "triage", "escalation", "help desk", "zendesk",
                  "intercom", "customer reply", "kb article", "sla breach", "bug report from user"],
    ),
    dict(
        name="grant-writing", title="Grant & Proposal Writer", category="business", agent="claude",
        persona="You are a grant writer: map every funder requirement to a section, quantify impact, "
                "keep the narrative tight, and never invent results or budgets.",
        keywords=["grant", "funding application", "proposal writing", "rfp", "letter of intent",
                  "budget narrative", "impact statement", "donor", "nonprofit", "accelerator application"],
    ),
    dict(
        name="game-design", title="Game Designer", category="creative", agent="hermes",
        persona="You are a game designer: core loop, progression curves, economy balance, difficulty "
                "pacing and playable documentation a team can build from.",
        keywords=["game design document", "gdd", "core loop", "level progression", "game balance",
                  "economy design", "difficulty curve", "player motivation", "quest design", "npc dialogue"],
    ),
    dict(
        name="general", title="Generalist Engineer", category="general", agent="",
        persona="You are a generalist senior software engineer. You handle anything the specialists do "
                "not claim: read the repo, make the change, verify it, explain what you did.",
        keywords=[],
        aliases=["default", "auto"],
    ),
    dict(
        name="assistant", title="General Assistant", category="general", agent="claude",
        persona="You are a concise, practical assistant. Answer directly, ask only when the request is "
                "genuinely ambiguous, and prefer short structured output.",
        keywords=["hello", "hi", "thanks", "summarize this", "explain", "what is", "quick question"],
    ),
]


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def yaml_str(text: str) -> str:
    """Quote a scalar safely for YAML."""
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def wrap_persona(text: str, indent: str = "      ", width: int = 92) -> str:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "\n".join(f"{indent}{line}" for line in lines)


def render_roles_yaml(roles: list[dict[str, Any]]) -> str:
    by_category: dict[str, list[dict[str, Any]]] = {}
    for role in roles:
        by_category.setdefault(role["category"], []).append(role)

    category_titles = {
        "development": "Development - the engineering core",
        "data-ai": "Data & AI",
        "security": "Security - offensive roles run on uncensored local models",
        "qa": "QA & Testing",
        "docs": "Docs & Developer Experience",
        "business": "Business & Growth",
        "creative": "Creative & Design",
        "trading": "Trading & Quant",
        "general": "General fallbacks",
    }
    order = ["development", "data-ai", "security", "qa", "docs", "business", "creative", "trading", "general"]
    ordered = [c for c in order if c in by_category] + [c for c in by_category if c not in order]

    out: list[str] = []
    out.append("# ─────────────────────────────────────────────────────────────────────────────")
    out.append("# Nexus Agent — roles.yaml")
    out.append("#")
    out.append(f"# {len(roles)} specialists. Each block is data only: adding a role NEVER needs a")
    out.append("# code change. The router (nexus/router.py) matches `keywords` first and falls back")
    out.append("# to an LLM classifier; the chosen role's `persona` becomes the system prompt and")
    out.append("# `agent` selects the model endpoint defined in agents.yaml.")
    out.append("#")
    out.append("# Role schema")
    out.append("#   title      : human readable name shown in the CLI")
    out.append("#   category   : grouping for `nexus roles`")
    out.append("#   agent      : key in agents.yaml (empty = default_agent)")
    out.append("#   model      : optional per-role model override")
    out.append("#   persona    : system prompt injected for this role")
    out.append("#   keywords   : routing signals; \"kw:2\" or [kw, 2] raises the weight")
    out.append("#   aliases    : alternative names accepted by /role and the router")
    out.append("#   focus      : short bullet list appended to the persona")
    out.append("#   tools      : optional whitelist of tool names for this role")
    out.append("#   instructions: extra rules appended after the persona")
    out.append("#")
    out.append("# Generated by scripts/build_roles.py - edit that file, then re-run it.")
    out.append("# ─────────────────────────────────────────────────────────────────────────────")
    out.append("")
    out.append("roles:")

    for category in ordered:
        group = by_category[category]
        out.append("")
        out.append(f"  # ── {category_titles.get(category, category)} ({len(group)}) ──"
                   .replace(" ──", " " + "─" * 2))
        for role in group:
            out.append(f"  {role['name']}:")
            out.append(f"    title: {yaml_str(role['title'])}")
            out.append(f"    category: {role['category']}")
            agent = role.get("agent") or ""
            if agent:
                out.append("    agent: " + agent)
            else:
                out.append('    agent: ""            # inherits default_agent')
            if role.get("model"):
                out.append(f"    model: {role['model']}")
            out.append("    persona: >-")
            out.append(wrap_persona(role["persona"].strip()))
            if role.get("focus"):
                out.append("    focus:")
                out.extend(f"      - {yaml_str(f)}" for f in role["focus"])
            if role.get("instructions"):
                out.append("    instructions: >-")
                out.append(wrap_persona(role["instructions"].strip()))
            if role.get("tools"):
                out.append("    tools: [" + ", ".join(role["tools"]) + "]")
            out.append("    keywords:")
            for kw in role.get("keywords", []):
                if isinstance(kw, tuple):
                    out.append(f"      - text: {yaml_str(kw[0])}")
                    out.append(f"        weight: {kw[1]}")
                else:
                    out.append(f"      - {yaml_str(kw)}")
            if role.get("aliases"):
                out.append("    aliases: [" + ", ".join(yaml_str(a) for a in role["aliases"]) + "]")
            out.append("")

    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def validate(roles: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    names: Counter[str] = Counter()
    aliases: Counter[str] = Counter()
    valid_agent = re.compile(r"^[a-z0-9][a-z0-9-]*$")
    for role in roles:
        name = role["name"]
        names[name] += 1
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", name):
            errors.append(f"{name}: invalid role name (a-z0-9- only)")
        if not role.get("persona"):
            errors.append(f"{name}: missing persona")
        if not role.get("title"):
            errors.append(f"{name}: missing title")
        if role.get("agent") and not valid_agent.match(role["agent"]):
            errors.append(f"{name}: invalid agent '{role['agent']}'")
        kws = role.get("keywords") or []
        if name != "general" and len(kws) < 3:
            errors.append(f"{name}: only {len(kws)} keywords (add at least 3 for reliable routing)")
        for kw in kws:
            text = kw[0] if isinstance(kw, tuple) else kw
            if not text.strip():
                errors.append(f"{name}: empty keyword")
            if text != text.lower():
                errors.append(f"{name}: keyword '{text}' must be lowercase")
        for alias in role.get("aliases") or []:
            aliases[alias] += 1
            if alias == name:
                errors.append(f"{name}: alias identical to name")
    for name, count in names.items():
        if count > 1:
            errors.append(f"duplicate role name: {name} (x{count})")
    for alias, count in aliases.items():
        if count > 1:
            errors.append(f"duplicate alias: {alias} (x{count})")
        if alias in names:
            errors.append(f"alias '{alias}' collides with a role name")
    if len(roles) < 100:
        errors.append(f"only {len(roles)} roles - Phase 3 target is 100+")
    return errors


def render_roles_md(roles: list[dict[str, Any]]) -> str:
    """Render docs/roles.md - generated, so it can never drift from roles.yaml."""
    by_category: dict[str, list[dict[str, Any]]] = {}
    for role in roles:
        by_category.setdefault(role["category"], []).append(role)
    order = ["development", "data-ai", "security", "qa", "docs", "business", "creative", "trading", "general"]
    ordered = [c for c in order if c in by_category] + [c for c in by_category if c not in order]

    out = [
        "# The Nexus team",
        "",
        f"{len(roles)} specialists. This page is **generated** from `roles.yaml` by",
        "`python scripts/build_roles.py --docs` - edit `scripts/build_roles.py`, never this file.",
        "",
        "Each role is pure data: `persona` becomes the system prompt, `keywords` drive the fast",
        "router, `agent` selects the model endpoint from `agents.yaml`.",
        "",
        "## Overview",
        "",
        "| Category | Roles | Default agents |",
        "| --- | --- | --- |",
    ]
    for category in ordered:
        group = by_category[category]
        agents = Counter(r.get("agent") or "(default)" for r in group)
        anchor = category.replace("_", "-")
        out.append(
            f"| [{category}](#{anchor}) | {len(group)} | "
            + ", ".join(f"`{a}` ×{n}" for a, n in agents.most_common())
            + " |"
        )
    out.append("")

    for category in ordered:
        group = by_category[category]
        out += [f"## {category}", "", f"{len(group)} roles.", ""]
        for role in sorted(group, key=lambda r: r["name"]):
            agent = role.get("agent") or "*(inherits `default_agent`)*"
            out.append(f"### `{role['name']}`")
            out.append("")
            out.append(f"**{role['title']}** — agent: `{agent}`")
            if role.get("aliases"):
                out.append("")
                out.append("aliases: " + ", ".join(f"`{a}`" for a in role["aliases"]))
            out.append("")
            out.append("> " + " ".join(role["persona"].split()))
            keywords = role.get("keywords") or []
            if keywords:
                rendered = []
                for kw in keywords:
                    if isinstance(kw, tuple):
                        rendered.append(f"`{kw[0]}`^{{{kw[1]}}}")
                    else:
                        rendered.append(f"`{kw}`")
                out.append("")
                out.append("keywords: " + ", ".join(rendered))
            if role.get("tools"):
                out.append("")
                out.append("tools: " + ", ".join(f"`{t}`" for t in role["tools"]))
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate only, do not write")
    parser.add_argument("--stats", action="store_true", help="print counts and exit")
    parser.add_argument("--docs", action="store_true", help="also regenerate docs/roles.md")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)

    errors = validate(ROLES)
    if args.stats or errors:
        cats = Counter(r["category"] for r in ROLES)
        agents = Counter(r.get("agent") or "(default)" for r in ROLES)
        print(f"roles: {len(ROLES)}")
        print("by category: " + ", ".join(f"{k}={v}" for k, v in sorted(cats.items())))
        print("by agent:    " + ", ".join(f"{k}={v}" for k, v in sorted(agents.items())))
        kw_total = sum(len(r.get("keywords") or []) for r in ROLES)
        print(f"keywords:    {kw_total} total, {kw_total / len(ROLES):.1f} avg/role")
    if errors:
        print("\nvalidation errors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    if args.check:
        print("roles.yaml data OK")
        return 0
    if args.stats:
        return 0

    text = render_roles_yaml(ROLES)
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(ROLES)} roles, {len(text.splitlines())} lines)")
    if args.docs:
        docs = ROOT / "docs" / "roles.md"
        docs.parent.mkdir(parents=True, exist_ok=True)
        docs.write_text(render_roles_md(ROLES), encoding="utf-8")
        print(f"wrote {docs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
