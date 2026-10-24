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
