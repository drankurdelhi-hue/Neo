# ns-sync — NeoSapien to WhatsApp bridge

NeoSapien ka data WhatsApp self-chat me push karta hai, taaki Perisclaw use apni normal chat history ki tarah padh sake.

Do tarah se chal sakta hai:

```
Routines (default, koi server nahi):
  NeoSapien  -->  Claude Routine  -->  Perisclaw  -->  WhatsApp self-chat  -->  Perisclaw agent

Script (real-time listen chahiye to):
  NeoSapien MCP  -->  ns_sync.py  -->  Perisclaw  -->  WhatsApp self-chat  -->  Perisclaw agent
```

Dono me WhatsApp tak pahunchane wala Perisclaw hi hai — wahi gateway hai.

---

## 1. Ye design kyun hai

Pehla instinct ye hota hai ki Perisclaw ko seedha NeoSapien se jodo. **Wo possible nahi hai.**

- Perisclaw ke event rules me sirf 6 actions hain: `send_message`, `react`, `forward`, `create_task`, `delete`, `mark_read`. **Koi HTTP call step nahi.**
- Perisclaw me external MCP server add karne ka koi option nahi. Uska agent sirf apne WhatsApp tools chalata hai.
- Claude chat ka output bhi automatically Perisclaw tak nahi jaata. Koi feed nahi hai.
- NeoSapien MCP **read-only** hai. Reminder assign/update/complete karne ka koi tool exist nahi karta.

Isliye bridge chahiye hi chahiye. Lekin agar data WhatsApp ke **andar** daal diya jaye, to Perisclaw ko kuch configure nahi karna padta — wo `search_messages` aur `wa_db_query` se usko waise hi padh leta hai jaise koi bhi purana message.

Ye "zero config on Perisclaw" hai, "zero infrastructure" nahi. Ek machine chahiye jo hamesha on rahe. Chrome pe ye nahi chal sakta — browser me cron nahi hota, aur tab band hote hi sab ruk jaata hai.

---

## 2. Verified API facts

Ye sab live test karke confirm kiya gaya hai. Guess mat karna, yahi use karna.

### 2.1 `get_messages` response

```json
{"ok": true, "chat_id": "...", "count": 3, "messages": [{
  "message_id": "3EB0F63F8EE6BE30070303",
  "sender": "910000000000@s.whatsapp.net",
  "sender_identity": {"contact_id": "...", "lid": "...@lid", "name": "Account Owner", "pushname": null},
  "@sender_mention": "@910000000000",
  "from_me": true,
  "performed_by": "perisclaw",
  "body": "...",
  "message_type": "chat",
  "timestamp": "2026-09-13T11:49:32.000Z"
}]}
```

### 2.2 `performed_by` — sabse important field

Self-chat me **har message ka `from_me: true`** hota hai. Operator ka apna message, Perisclaw ka reply, aur script ka apna reply — teeno. `from_me` se filter karna bekaar hai.

Sahi filter:

| `performed_by` | Kisne bheja | Kya karna hai |
|---|---|---|
| `null` / `""` | Human ne phone pe type kiya | **Process karo** |
| `"perisclaw"` | Perisclaw agent | Skip |
| koi aur value | API / script | Skip |

Ye filter na ho to script apne hi output ko command samajh kar dobara chalayega — **infinite loop**. `do_listen()` me ye lagaya gaya hai.

Perisclaw apne replies ke aage `> 🦀 Perisclaw:` prefix bhi lagata hai. `performed_by` filter isko cover kar leta hai, par debug karte waqt kaam ka hai.

### 2.3 Timestamp format — do jagah do format

| Source | Format |
|---|---|
| `get_messages`, `search_messages` | ISO 8601 string — `"2026-09-13T11:49:32.000Z"` |
| `wa_db_query` (`messages.timestamp`) | epoch **milliseconds** integer |

`wa_db_query` me ISO se filter karna ho: `WHERE timestamp >= unixepoch('2026-09-13T00:00:00Z') * 1000`

### 2.4 WhatsApp markup — Markdown NAHI hai

- `*bold*` — single asterisk. `**double**` kabhi nahi.
- `_italic_`, `~strike~`, `` `code` ``
- `#` headers kaam nahi karte
- `[text](url)` kaam nahi karta — raw URL paste karo, WhatsApp khud link bana deta hai
- **Em dash (—) use mat karo.** `clean()` function ise normal hyphen me badal deta hai.
- @mention ke liye `@910000000000` (JID ke digits, country code chhed-chaad ke bina)

### 2.5 `message_send` ka `chat_id`

- DM: `<number>@s.whatsapp.net`
- Group: `<id>@g.us`
- Self-chat: literal string `"self"` chalta hai (bhejne ke liye). Par **`get_messages` ke liye asli JID chahiye** — `"self"` wahan kaam nahi karta.

### 2.6 Self-chat JID

```
<country code><number>@s.whatsapp.net
```

Apna asli JID `.env` me `SELF_JID` me bharo. Repo me kabhi nahi.

---

## 3. NeoSapien data model

### 3.1 Owners — ek hi hai

`search_owners` sirf **ek** owner return karta hai — account holder khud. Account personal hai.

Iska matlab: reminder ka `owner` field (koi bhi naam jo transcript me suna gaya) **koi real user record nahi hai**. AI ne transcript se naam sun kar likh diya hai. Wo free text hai, ID nahi.

Iska seedha nateeja — **NeoSapien me naam confirm karna possible nahi**. Koi write tool hi nahi hai. Name-to-contact mapping bahar maintain karni padegi.

### 3.2 Reminder object

```json
{
  "id": "uuid",
  "task_name": "...",
  "details": "...",
  "owner": "Some Name",          // free text, AI-extracted
  "owner_ids": [],               // hamesha khali
  "status": "pending",
  "approval_status": "pending",  // AI-generated, kisi ne review nahi kiya
  "priority": "low|medium|high|critical",
  "due_date": "2026-08-18T05:30:00+05:30",
  "due_inferred": true,          // AI ne guess kiya, kisi ne bola nahi
  "who_inferred": true,          // owner bhi AI ne guess kiya
  "ai_generated": true,
  "source_memory_ids": ["..."],
  "created_at": "..."
}
```

**`status` pe bharosa mat karo.** Koi task complete mark nahi karta, isliye `pending` ka matlab sirf "kabhi banaya gaya tha" hai — "abhi bhi khula hai" nahi.

`due_inferred: true` aur `who_inferred: true` ka matlab hai AI ne wo field khud guess kiya hai. Aise tasks ko automatic assign karna khatarnak hai.

### 3.3 Memory object

```json
{
  "memory": {
    "_id": "uuid",
    "title": "...",
    "summary": "...",
    "topics": [], "domains": [], "emotions": [],
    "participants": [], "mentioned_entities": [],
    "created_at": "...", "started_at": "...", "finished_at": "...",
    "source": "neo1"
  },
  "user": {"id": "...", "name": "Account Owner"}
}
```

Dhyan do: memory object ek wrapper ke andar `memory` key me nested hai. Seedha `item["title"]` nahi chalega.

### 3.4 Pagination

- `get_reminders` — `{"pagination": {"has_more": bool, "total_count": n, "page": n}}`
- `search_memories` — `{"total_pages": n, "page": n, "total_found": n}`

Dono alag shape hain. Code me dono handle kiye hain.

### 3.5 Current volumes (13 Sept 2026)

- **571 memories** (23 July se)
- **130 reminders**, sab `pending`
- Aaj ki 23 memories

---

## 4. Data quality — jo samajhna zaroori hai

### 4.1 Assigned backlog purana hai

Sabse naya assigned task **24 August** ka hai. September ke saare reminders `Unassigned` hain. Bina soche bhejoge to logon ke paas 3-4 hafte purane task bina context ke chale jayenge.

Kuch to mar chuke hain — jaise ek survey link jo usi din expire ho raha tha, ek "banda cabin me wait kar raha hai" wala turant-kaam, ek document handover jo hafte pehle ho chuka. Aise task aaj bhejne ka koi matlab nahi, ulta confusion hoti hai.

### 4.2 Owner names me se kai log hain hi nahi

Ye "owner" values actually role labels hain, banda nahi:
`Interior Designer`, `Furniture Vendor`, `Developer`, `Speaker 2`, `Unassigned`

Inhe permanently block karo.

### 4.3 Contact matching — sabse bada khatra

WhatsApp me **6,463 contacts hain, 4,730 me naam hai**. Fuzzy name matching yahan bharosemand nahi. Asli data pe check karne pe ye pattern mila:

| NeoSapien naam ka type | WhatsApp matches |
|---|---|
| Common first name | 15-20+ matches — staff, patients, vendors, sab mile hue |
| Naam + surname | 2-3 numbers, kaun sa current hai pata nahi |
| Ghar ka naam | 8+ (rishtedaar, dost, same naam) |
| Kuch naam | **0 matches** — wo banda WhatsApp me hi nahi hai |
| Galat suna naam | 0 — AI ne transcript me milta-julta naam suna, asli naam thoda alag hai |

Clinic ke patients ek naming prefix se save hain, aur wo prefix common first names ke saath overlap karta hai. Automatic matching kisi patient ko internal task bhej degi.

**Isliye: fuzzy matching mat use karo. Ek chhoti manual whitelist rakho.**

### 4.4 Whitelist

Asli whitelist `contacts.json` me hai — **wo file gitignored hai**, kyunki usme logon ke asli phone numbers hote hain. Structure dekhne ke liye `contacts.example.json` dekho.

- `jid` set = resolved, message ja sakta hai
- `jid: null` = **blocked**, kabhi message mat bhejo (role labels, transcription artifacts, aur apna khud ka naam)

Jo naam abhi confirm nahi hue, unhe `--approve-names` se ek-ek karke resolve karo. Jab tak resolve nahi hote, bridge unhe skip karta rehta hai — wahi safe default hai.

---

## 5. Files

| File | Kaam |
|---|---|
| `ns_sync.py` | Poora logic — MCP client, push mode, listen mode |
| `deploy.sh` | VPS install: user, venv, systemd, cron |
| `ns-sync.service` | systemd unit for `--listen`, `Restart=always` |
| `env.example` | Tokens ka template, `.env` isse banao |
| `ns_peri_bridge.py` | Alag script — reminders ko owner ke WhatsApp pe bhejta hai, interactive name approval ke saath |
| `routines/*.txt` | Claude Routines ke ready prompts (server-less rasta, section 6a) |
| `contacts.example.json` | Whitelist ka template (ye repo me hai) |
| `contacts.json` | Asli name-to-JID whitelist — **gitignored**, sirf VPS pe |

Repo me kabhi nahi jaate: `.env` (tokens), `contacts.json` (asli numbers), `sync_state.json`, `sent_reminders.json`.

---

## 6. Deployment

Do raste hain. **Routines wala rasta default hai** — usme koi server nahi chahiye.

### 6a. Claude Routines (koi server nahi)

Yahan script chalti hi nahi. Claude khud bridge ka kaam karta hai: scheduled Routine fire hoti hai, Claude NeoSapien connector se data leta hai aur Perisclaw connector se self-chat me daal deta hai. Koi token, koi `.env`, koi cron, koi systemd.

Prompts `routines/` folder me ready hain:

| File | Suggested schedule |
|---|---|
| `routines/daily-memories-digest.txt` | roz 21:00 local |
| `routines/hourly-ns-listener.txt` | har ghanta |

Setup:
1. claude.ai → Routines → new Routine
2. Poori `.txt` file ka content prompt me paste karo
3. **NeoSapien aur Perisclaw connectors attach karo** — ye step chhoda to Routine fire hogi par kaam nahi karegi, kyunki fired session ke paas `mcp__*` tools nahi honge
4. `hourly-ns-listener.txt` me `SELF_JID` ki jagah apna asli JID daalo (wo file me placeholder hai, kyunki ye repo public hai)

Routine CLI/MCP se banane ki koshish mat karo jab tak `connectors` parameter aapke org pe enabled na ho — warna connector-less Routine banti hai jo chupchap fail hoti hai.

**Jo Routines se nahi hota:** `--listen` ka real-time polling. Routines ka minimum interval normally 1 ghanta hai aur firings ke beech koi process zinda nahi rehta, to WhatsApp me `/ns` likhne pe jawab 0-60 min late aata hai. Sub-minute chahiye to 6b.

Aur `ns_peri_bridge.py` (reminders seedha owner ko bhejna) Routines se nahi chalta — wo interactive approval maangta hai, to usko manually chalao.

### 6b. VPS (real-time listen ke liye)

Contabo VPS pe (wahi jahan CRM chal raha hai):

```bash
sudo ./deploy.sh              # .env banayega, phir ruk jayega
sudo nano /opt/ns-sync/.env   # tokens + SELF_JID bharo
sudo ./deploy.sh              # dobara: systemd + cron chalu
```

### Environment

```bash
NEOSAPIEN_TOKEN=...
PERISCLAW_TOKEN=...
NEOSAPIEN_MCP_URL=https://api.neosapien.xyz/mcp
PERISCLAW_MCP_URL=https://api.perisclaw.com/mcp
SELF_JID=<country code><number>@s.whatsapp.net
```

### Do modes

**Push** (cron):
```bash
python ns_sync.py --push today      # aaj ki memories
python ns_sync.py --push week       # 7 din
python ns_sync.py --push tasks      # pending reminders, owner ke hisaab se grouped
python ns_sync.py --push summary    # NeoSapien daily summary
```

**Listen** (systemd daemon):
```bash
python ns_sync.py --listen --poll 20
```

Self-chat me type karo:
```
/ns today
/ns week
/ns tasks
/ns summary
/ns search haridwar furniture
```

### Cron (UTC me likha hai — Contabo default)

```
30 15 * * *   --push today     # 21:00 IST
30 3  * * 1   --push tasks     # Monday 09:00 IST
30 14 * * 0   --push week      # Sunday 20:00 IST
```

Server TZ IST pe set hai to adjust karo.

**Dono chalao.** Push se history bharti rahegi (Perisclaw ko search karne ka material milta rahega), listen se turant kuch bhi mangwa sakte ho.

### `ns_peri_bridge.py` — actual reminder delivery to owners

```bash
python ns_peri_bridge.py --dry-run        # kya bhejega, bina bheje dikhao
python ns_peri_bridge.py --approve-names  # naye owner names ko WhatsApp contact se match karo, ek-ek karke
python ns_peri_bridge.py --send           # bhejo (sirf resolved + whitelisted owners ko)
```

- Sirf `contacts.json` me resolved (`jid` set) naam ko bhejta hai. Unknown ya blocked naam skip.
- `who_inferred` + `due_inferred` dono `true` wale tasks by default skip hote hain (`--include-inferred` se force karo).
- 14 din se purane `due_date` wale tasks by default skip hote hain (`--include-stale` / `--stale-days` se adjust karo).
- `sent_reminders.json` me bheja hua reminder ID track hota hai, taaki dobara na jaaye.

---

## 7. Code notes

- `MCPClient` — streamable-HTTP MCP client. `initialize` se session lo (`Mcp-Session-Id` header), phir `tools/call`. SSE aur plain JSON dono responses handle karta hai.
- `send_chunks()` — lambe digest ko 4000-char messages me todta hai, `(1/3)` footer ke saath. WhatsApp ki apni limit zyada hai, par padhne layak rakhne ke liye 4000 rakha hai.
- `sync_state.json` — listen mode ka `last_seen_ts`, taaki ek command do baar process na ho.
- `clean()` — em dash hata deta hai, WhatsApp markup safe banata hai.
- `SELF_NAMES` — generic owner values jo kabhi real recipient nahi hote (`unassigned`, `self`, khali). Apna khud ka naam yahan code me nahi, `contacts.json` me `jid: null` entry ke roop me daalo — is tarah personal naam public repo se bahar rehta hai.

---

## 8. Failure modes

| Problem | Kya check karo |
|---|---|
| Listen mode commands miss kar raha | `performed_by` filter. Response print karke dekho ki human message me wo field kya aa rahi hai. |
| Script apne hi replies process kar raha | Wahi `performed_by` filter. Ye pehle se lagaya hua hai, hataana mat. |
| Timestamp comparison fail | `get_messages` ISO deta hai, `wa_db_query` epoch-ms. Confuse mat karo. |
| Message me `**bold**` dikh raha | WhatsApp Markdown nahi hai. Single asterisk use karo. |
| Galat bande ko task gaya | Fuzzy matching on ho gayi hogi. Sirf whitelist use karo. |
| Auth error | Token `.env` me, chmod 600. Dono servers ko alag token chahiye. |

---

## 9. Security

- Tokens sirf `/opt/ns-sync/.env` me (chmod 600). Code me kabhi nahi.
- **`contacts.json` gitignored hai.** Usme dusre logon ke asli phone numbers hain — public repo me wo kabhi commit nahi hone chahiye. Repo me sirf `contacts.example.json` (dummy numbers) jaata hai.
- Listen mode **sirf `SELF_JID`** sunta hai. Isko kabhi mat kholo — warna koi bhi chat se `/ns` likh kar aapka NeoSapien data mangwa sakta hai.
- systemd unit me `ProtectSystem=strict`, `PrivateTmp`, `NoNewPrivileges` lage hain. Hataana mat.
- Push self-chat me jaata hai, kisi aur ke paas nahi.

---

## 10. Khule kaam

- **Baaki naam confirm karna** — ~8 naam abhi unresolved hain. Ek-ek karke `--approve-names` mode se, aur result `contacts.json` me (repo me nahi).
- **Kuch naam WhatsApp me hi nahi hain.** Ya to un logon ke numbers add karo, ya un tasks ko `jid: null` se block kar do.
- **Stale backlog.** 130 reminders me se zyada tar purane hain. Blanket send se pehle ek baar saaf karo.
- **Two-way sync possible nahi** — NeoSapien read-only hai. Reply aane pe reminder complete mark nahi kar sakte. Completion tracking alag rakhni padegi.
- **Self-chat bhar jayega.** Roz ka push matlab mahine me ~30 lambe messages. Perisclaw ke search ke liye achha, manually scroll karne ke liye nahi. Chaho to daily ki jagah weekly kar do.
