# Premium Subscription Bot (Telegram)

Flow: /start -> product button -> price + terms -> Buy -> screenshot -> admin Approve (duration select) -> user ko 1-time invite link -> expiry par auto kick + renew notification.

## Setup
1. @BotFather se bot banao, token lo.
2. Admin ka Telegram ID @userinfobot se lo.
3. Bot ko har premium channel mein **admin** banao (permissions: Ban users + Invite users via link).
4. Channel ID (-100...) pata karo (channel ka message @userinfobot/@RawDataBot ko forward karo).
5. `bot.py` ke upar PRODUCTS mein naam/price/terms edit karo.

## GitHub
git init && git add . && git commit -m "init" && git push (repo bana ke)

## Railway
1. New Project -> Deploy from GitHub repo.
2. Variables mein `.env.example` wale sab daalo.
3. **Volume** add karo, mount path `/data` (warna redeploy par database delete ho jayega), aur `DB_PATH=/data/bot.db` rakho.
4. Service type worker hai (Procfile), koi port nahi chahiye.

## Commands
User: /start, /mysubs
Admin: /subs (active list), /stats, /revoke <user_id> <movies|tv|courses|music|db>
