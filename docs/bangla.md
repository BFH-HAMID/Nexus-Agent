# Nexus: বাংলায় দ্রুত গাইড

এই ফাইলটা ছোট করে বলে — কী কী যোগ হলো, আর কীভাবে ব্যবহার করবেন।

## ১. ইনস্টল ও চালু

```bash
cd Nexus-Agent
git pull                 # main-এ সব মার্জ হয়ে গেছে
pip install -e ".[dev]"  # `nexus` কমান্ডটা ইনস্টল হয়
nexus                    # শুধু এটুকু — এজেন্ট চালু
```

প্রথমবার একবার কী সেট করবেন (কোনো `.env` ফাইল লাগে না):

```bash
nexus keys set gemini      # ফ্রি টিয়ার, সবচেয়ে দ্রুত
# অথবা
nexus keys set deepseek
nexus keys set openrouter
```

## ২. GUI (ব্রাউজার ড্যাশবোর্ড)

টার্মিনাল থেকেই:

```bash
nexus gui            # ড্যাশবোর্ড চালু + URL দেখায়
nexus gui --no-open  # শুধু চালু, URL নিজে খুলবেন
```

চ্যাট চলাকালীন `/gui` লিখলেও একই জিনিস হয়। ডান দিকের নিচে
**LEARNING BRAIN** কার্ডে দেখা যাবে — কতগুলো skill, কতগুলো আগের রান, আর
কতগুলো মেমোরি জমেছে; সেখান থেকেই পুরোনো কাজ সার্চ করা যায়।

## ৩. নতুন জিনিস: এজেন্ট নিজে শিখে

তিনটা স্তরে জ্ঞান জমে:

| স্তর | কোথায় | কী থাকে |
| --- | --- | --- |
| **Skill** | `skills/`, `~/.config/nexus/skills/`, `<project>/.nexus/skills/` | কীভাবে একটা কাজ করতে হয় (ধাপ, কমান্ড, ভুল যেখানে হয়) |
| **Memory** | `~/.config/nexus/memories/MEMORY.md`, `USER.md` | সিদ্ধান্ত, তথ্য, আপনার পছন্দ |
| **Session** | `~/.config/nexus/sessions.db` | প্রতিটা রানের ইতিহাস (সার্চ করা যায়) |

কীভাবে শেখে:

1. কাজ শুরুর আগে প্রাসঙ্গিক skill আর মেমোরি সিস্টেম প্রম্পটে যায়।
2. কাজ শেষে (কমপক্ষে ৪টা টুল কল হলে) একটা সস্তা মডেল রিভিউ করে — দরকার হলে
   **একটা skill**, **একটা patch**, আর সর্বোচ্চ **তিনটা মেমোরি** লেখে।
3. পুরোনো/অব্যবহৃত skill কখনো ডিলিট হয় না — `archive` হয়।

## ৪. কমান্ড

```bash
nexus skills                 # শেখা skill-গুলোর তালিকা
nexus skills show <name>     # পুরো SKILL.md
nexus skills import          # Nous Research-এর hermes-agent থেকে ২০০+ skill আনো
nexus skills curate --apply  # পুরোনো skill আর্কাইভ করো
nexus learn "কীভাবে রিলিজ করি"   # এই কাজটাকে skill বানাও
nexus recall flaky test      # আগের রানগুলোতে সার্চ
nexus memory                 # MEMORY.md + USER.md
nexus memory add "এই রেপো fly.io-তে ডিপ্লয় হয়"
nexus memory add "উত্তর বাংলায় চাই" --user
nexus hermes                 # hermes-agent ব্রিজের স্ট্যাটাস
nexus update                 # Nexus নিজেই আপডেট করে নেবে
nexus doctor                 # সব চেক (skill/memory/session সহ)
```

চ্যাটের ভিতরে:

```
/help
/skills
/skill paginate-endpoint add cursor pagination to /users
/paginate-endpoint do the same thing      (শর্টকাট)
/learn কীভাবে একটা রোল যোগ করি
/curate
/recall pagination
/memory global
/gui
```

## ৫. Hermes রেপোটা এল কীভাবে

পুরো hermes-agent ফর্ক করা হয়নি (সেটা ২০ হাজার ফাইলের আলাদা অ্যাপ)। তার
**মূল ধারণাগুলো** Nexus-এর ভিতরে নিজের ভাষায় বসানো হয়েছে — skill, মেমোরি,
`/learn`, নাডজ, কিউরেটর, সেশন সার্চ, নিজে নিজে আপডেট। আর যে অংশগুলো এখনো
পোর্ট হয়নি (Telegram/Discord গেটওয়ে, Hermes-এর TUI, Electron অ্যাপ), সেগুলো
দরকার হলে `nexus hermes run` দিয়ে আসল `hermes` কমান্ডেই পাঠানো যায়।

## ৬. বন্ধ করতে চাইলে

```bash
NEXUS_LEARNING=0 nexus        # শেখা বন্ধ
NEXUS_SKILLS=0 nexus          # skill ইনজেকশন বন্ধ
```

`agents.yaml`-এর `settings:` ব্লকে স্থায়ীভাবেও সেট করা যায়।

## GUI-তে Ollama ও GitHub connect

`nexus gui` → **Connections & local models** → **Refresh models** → নিজের
downloaded model বেছে task চালান। একই controls VS Code sidebar-এও আছে।
Custom/uncensored model-সহ Ollama-তে থাকা যেকোনো model দেখাবে। Selected model
ওই run-এর roles, router ও learning চালায়; cloud fallback নেই। Native tool
support না থাকলে text-mode JSON tools ব্যবহার হয়, তবে model-এর
instruction-following ক্ষমতার উপর tool reliability নির্ভর করে। Demo mode-এ
আসল model call হয় না।

Model না দেখালে Nexus server-এর machine-এ `ollama list` দিয়ে দেখুন এবং Ollama
বন্ধ থাকলে `ollama serve` চালান। Default host `http://localhost:11434`; অন্য host
হলে Nexus চালানোর আগে `OLLAMA_HOST` environment variable দিন। Browser এবং Nexus
server আলাদা machine-এ হলে browser-এর local Ollama নিজে থেকে পাওয়া যাবে না।

GitHub CLI install করে **Nexus server-এর machine-এর terminal**-এ চালান:

```sh
gh auth login --hostname github.com --web
gh auth setup-git
gh auth status
```

Browser sign-in শেষ করে GUI-তে **Check connection** চাপুন। Connected username
দেখাবে; Nexus-এ password/token paste করার প্রয়োজন নেই। Repo permissions ও write
approval আগের মতোই থাকবে।

VS Code extension source থাকা মানে installed নয়:

```sh
cd vscode-extension
npm install
npm run package
```

VS Code → Extensions → `…` → **Install from VSIX** → তৈরি হওয়া `.vsix` বেছে নিন।
Nexus activity-bar icon খুলুন। Selection পাঠানো, current file নিয়ে প্রশ্ন,
role/pipeline picker, event stream এবং approval controls আছে।

## Cyberpunk CORE interface

`nexus`-এর banner এখন binary NEXUS logo ও `[NEXUS_CORE@SYSTEM:~]$` prompt
দেখায়। Interactive terminal-এ ছোট boot progress আছে; pipe/CI-তে animation নেই।
`NEXUS_NO_ANIMATION=1` দিয়ে বন্ধ করা যায়। Banner-এর role count registry থেকে আসে,
এটি online model count নয়।

Standalone Windows PowerShell launcher (repository root থেকে):

```powershell
.\scripts\nexus-core.ps1
# offline scripted tasks:
.\scripts\nexus-core.ps1 -Demo -NoAnimation
```

Launcher-এর `[SIM]` status ও 1000-agent header শুধুই visual simulation। User task
আসল `nexus task` command-এ যায় (আগে Nexus install/venv activate করুন)। `/gui`,
`/doctor`, `/help`, `/exit` support আছে; input PowerShell code হিসেবে evaluate হয় না।

`nexus gui`-তে neon glass dashboard, world-map overlay, binary rain, rotating globe
ও cognition map আছে। Map-এর খালি জায়গায় drag করে tilt করুন; **reset view** দিয়ে
ফিরুন। Tab দিয়ে node focus করে Enter/Space দিয়েও select করা যায়। OS reduced-motion
preference মানা হয়। Map কেবল configured model দেখায়—Claude বা অন্য model ব্যবহার
করতে তার provider/model config লাগবে; decorative globe/world map বাস্তব network
traffic নয়। এটি ব্যবহারযোগ্য dashboard, monitor/keyboard-এর স্থির photorealistic
ছবি নয়।

## Dashboard approval ও live-only data (সর্বশেষ পরিবর্তন)

- Approval panel এখন live log-এর উপরে, ডান পাশের scroll rail-এর মধ্যে নয়।
  **Approve** একবার tool চালানোর অনুমতি দেয়; **allow for this run** শুধু বর্তমান
  run-এ ওই tool অনুমোদন করে; **Deny** tool call প্রত্যাখ্যান করে। Read-only mode
  এবং dangerous-command restrictions আগের মতোই কার্যকর।
- Request ব্যর্থ হলে approval panel বন্ধ হয় না; error দেখায় এবং retry করা যায়।
  Page refresh করলে একই browser tab-এর চলমান task ও pending approval ফিরে আসে,
  যতক্ষণ Nexus server restart হয়নি। এটি server restart-এর পর session resume নয়।
- Side card-গুলো আর ছোট হয়ে controls লুকায় না—ডান/বাম column scroll করুন।
- Run চলাকালে Clear disabled থাকে। Stop পরের tool শুরু হওয়া আটকায়; ইতিমধ্যে
  চলমান model request/command ফিরতে সময় লাগতে পারে, তাই তা সঙ্গে সঙ্গে kill করার
  দাবি UI করে না। Pipeline-তেও read-only checkbox সব step-এ প্রযোজ্য।
- Dashboard থেকে simulated traffic, globe nodes ও geographic connection-এর
  বানানো তথ্য সরানো হয়েছে। Counters/graphs server event ও measured tool duration
  থেকে আসে; geolocation/threat scan সংগ্রহ করা হয় না, তাই unavailable বলা হয়।
  Configured endpoint মানে model online আছে এমন নয়।
- Dashboard task API এখন demo server-এ task চালায় না। `--demo` ছাড়া চালান, এবং
  environment-এ `NEXUS_DEMO` থাকলে সরান। CLI-এর আলাদা offline demo এখনও আছে।

Update করা version চালানোর পর আগের server বন্ধ করে আবার `nexus gui` চালান এবং
browser-এ `Ctrl+F5` চাপুন। নিরাপদ পরীক্ষার task: `Create hello.txt containing Hello Nexus`।

## নতুন Workbench: ২০টি dashboard update

Dashboard-এর উপরের **Workbench** button বা **Ctrl+K** চাপুন। এখন পাওয়া যাবে:

- Provider reachability check, per-role model mapping ও server-enforced local-only policy।
- Approval-এর আগে file diff; Plan → review/edit → execute; স্থায়ী run history।
- Serial task queue, reorder/pause/cancel ও পর্যালোচনার পরে failed-step retry।
- Allowed write-folder policy, changed-file content/diff viewer।
- Ollama installed/loaded model manager, real download status, confirmed delete,
  capability metadata ও ইচ্ছা করলে ছোট formatting/speed inference test।
- আসল server CPU/RAM, supported NVIDIA GPU data, token/timing observations,
  নিজের দেওয়া source/rate অনুযায়ী স্পষ্টভাবে চিহ্নিত cost estimate।
- Error explanations, Git branch/status, PR preview/test/push/create wizard,
  GitHub issues/checks/failed logs।
- Saved layout, card resize/hide/reorder, focus mode, বাংলা/ইংরেজি action labels ও
  searchable command palette। Technical output ও বিস্তারিত help মূল ইংরেজিতেই থাকে।

সব settings ও dashboard history `$NEXUS_HOME/dashboard.db`-তে workspace অনুযায়ী
থাকে; repo-তে নয়। Server restart হলে পুরোনো অসমাপ্ত কাজ history-তে interrupted
দেখাবে, নিজে থেকে আবার চালাবে না। Local-only হলো model-routing policy—OS firewall
নয়। Restricted write folders থাকলে shell/git write tools-ও refuse হয়, কারণ এগুলোকে
ওই folder-এর মধ্যে নির্ভরযোগ্যভাবে আটকে রাখা যায় না।

সঠিক model/GitHub/GPU configuration ছাড়া unavailable data বানিয়ে দেখানো হয় না।
সব ২০টি feature-এর ব্যবহার, confirmation ও সীমাবদ্ধতা: [`workbench.md`](workbench.md)।

### Studio: নির্বাচিত ৪৫টি ফিচার

Dashboard-এর **Studio** থেকে Sessions, Review, Evidence, Policy, Data,
Workflows, Inbox এবং Runtime পাওয়া যাবে। Visual workflow graph, conditional steps,
parallel read-only research, clarification, dev-app preview/logs, isolated execution,
reviewed memory/skills ও backup/migration যুক্ত আছে। প্রতিটি নির্বাচিত ফিচারের তালিকা
ও সীমা: [Studio guide](studio.md)।

**গুরুত্বপূর্ণ:** enforced container execution-এর জন্য Docker daemon ও আগে থেকে
ইনস্টল করা trusted image লাগে। OS-keystore encryption-এর জন্য `secure-state` extra
ও কার্যকর Secret Service/Keychain/Credential Vault লাগে। না থাকলে activation বন্ধ
থাকবে—host execution বা plaintext key file-এ গোপনে fallback করবে না। Worktree একা
OS sandbox নয়। Model-এর “সফল” দাবি আর প্রকৃত verification evidence আলাদা।

### Repository bar: Commit, Push, PR ও Merge

Dashboard-এর উপরে repository selector, current branch, প্রকৃত `+ / −` পরিবর্তন,
**Commit**, **Push**, **Create PR**, **PRs** ও **Merge PR** আছে। আগে preview দেখুন,
তারপর প্রদর্শিত confirmation হুবহু লিখে execute করুন। Repo select শুধু Git controls
বদলায়; agent workspace বা branch বদলায় না। Force push/auto-merge/admin bypass নেই।
বিস্তারিত: [Repository toolbar](repository-toolbar.md)।
