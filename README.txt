ADAPTIVE AUTO-ACCEPT BOT - SETUP

1. BotFather mein naya token banayein. Pehle jo token chats/code/logs mein dikh chuka hai use dobara mat chalayein.
2. .env.example ki copy bana kar copy ka naam .env rakhein. BOT_TOKEN= ke baad naya token paste karein. OWNER_ID pehle se diya hai; zarurat par apna numeric ID set karein.
3. Python 3.10+ install ho. PowerShell mein isi folder ke andar:
   pip install -r requirements.txt
   python main.py
4. Apne Telegram account se bot ko /start bhejein. Admin alerts aane ke liye pehle /start zaroor karein.
5. Bot ko channel admin banayein aur Invite Users / approve join requests permission dein. Naya admin milte hi bot owner ko channel aur admin dene wale user ka alert bhejega.

CHANNEL CONTROLS
- /start — naam ke saath welcome aur owner control panel
- /channels — registered channels
- /on CHANNEL_ID — kisi channel ko start
- /off CHANNEL_ID — kisi channel ko rokna; queue save rahegi
- /limit CHANNEL_ID 100 120 — channel ka daily random limit range
- /delay CHANNEL_ID 12 30 — channel ka delay range minutes mein
- Button panel se bhi channel, ON/OFF, daily range aur delay manage kar sakte hain.

DEFAULTS / OLD DATA
- Naye channels: 50–80 approvals per day, 1–180 minute delay range. Existing smart time/queue delay behavior isi range ke andar kaam karta hai; owner chahe to `/delay CHANNEL_ID 12 30` set kar sakta hai.
- Purana queue_data.json isi folder mein rakhein. Bot purane queues/channel_stats ko load karke naya settings data add karta hai.
- Saved purane channels startup par verify honge. Telegram bot API sab purane admin channels ki list nahi deta; jo channel old JSON mein saved nahi hai, usmein ek baar /register post karein. Verify hote hi owner ko notification aayega.
- Queue / OFF setting ke dauran save rahegi; ON karne par continue hogi. Daily target Asia/Kolkata date ke hisaab se reset hota hai.
- Bot token ko code ya logs mein mat rakhein. Is project mein httpx request-URL logging band hai. Ek token ke liye ek hi polling process chalayein.
