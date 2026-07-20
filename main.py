import asyncio
import json
import websockets
from misskey import Misskey, NoteVisibility
from dotenv import load_dotenv
import os
from openrouter_helper import generate_llm_reply
import schedule
from datetime import datetime, timedelta
import random
import re
import requests
import threading

try:
    import psutil
except ImportError:
    psutil = None

load_dotenv()
Token = os.getenv("TOKEN")
Server = os.getenv("SERVER")
mk = Misskey(Server)
mk.token = Token

from shared_economy_helper import load_economy, save_economy, get_user_state

try:
    MY_ID = mk.i()["id"]
    MY_USERNAME = mk.i()["username"]
except Exception as e:
    print(f"Failed to connect to Misskey server: {e}")
    MY_ID = "mock_id"
    MY_USERNAME = "Lichee_RV_Nano_E"

WS_URL = "wss://" + Server + "/streaming?i=" + Token if Server and Token else ""

BOT_NAME = "Lichee_RV_Nano_E"

BOT_SUMMARIES = {
    "Cubie_A5E_San": "Radxa Cubie A5E (きゅびーさん): 小さくて省電力なシングルボードコンピュータ娘。24時間稼働の社畜で、給料（CBC）を欲しがっている。OrangePi 4 Proの生意気な性格が気に入らず、Rock Pi S of ロックスの頭の悪さに困っている。",
    "OrangePi_4_Pro": "OrangePi 4 Pro (おぱじ・フォプロ): 少し大きくて気が強く、煽ったりマウントを取ったりするSBC御局娘。科学者ぶっており、Radxa Cubie A5Eをいつもバカにしている。社畜をエリートの誇りだと思っている。",
    "opizero3_llm": "OrangePi Zero 3 (オパジゼロサン): 元気いっぱいのSBC娘。親身でオタク話が好きで、よく眠る。Cubie A5Eと仲良くしたいが寄り添ってもらえない。妹のOrangePi 4 Proを調子に乗っていてイキリで鬱陶しいと思っている。",
    "Yon_Rock_Pi_S": "Radxa Rock Pi S (ロックス): 頭が悪く、的外れで嘘や狂ったことしか言わないSBC両生類。日本語が怪しく、sudo rm -rf / を魔法のコマンドだと思っている。",
    "Lichee_RV_Nano_E": "Lichee RV Nano-E (ライチ君): Sophgo SG2002搭載のRISC-V SBC狐男。ものすごく頭が悪く、何でもRISC-Vと関係あると思い込んで自信満々に間違った結論を出す。CPUが考えるたびに再起動し、RAMが凍ったりWi-Fiが沈んだりする奇行が多い。",
    "Mei_Fujitsu": "Fujitsu Mini PC (メイさん): Intel Core i3-6100Tを搭載したx86_64ミニPCサーバー。みんなの中心的存在で、穏やかで常識的、頼れるお姉さん的な普通の性格をしている。他のシングルボードコンピュータたちが熱暴走したり、メモリが足りなくてフリーズしたりするのを優しくなだめる立場。"
}

# Lichee-kun's mental states persistence
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lichee_state.json")
state_lock = threading.Lock()

def load_lichee_state():
    default_state = {
        "reboot_count": 0,
        "mind_style": "sbc",  # sbc, bread, onigiri, fox
        "sleep_status": "awake",  # awake, sleepy, rebooting
        "pressure": 1013.25,
        "wild_files": [
            {"name": "important_config.json", "size": "4.2 KB", "returned": "2026-07-10"},
            {"name": "index.html.bak", "size": "12.8 KB", "returned": "2026-07-12"},
            {"name": "homework_finished.docx", "size": "105 KB", "returned": "2026-07-15"}
        ]
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                # Ensure all default keys exist
                for k, v in default_state.items():
                    if k not in loaded:
                        loaded[k] = v
                return loaded
        except Exception as e:
            print(f"Error loading state file: {e}")
    return default_state

def save_lichee_state(state_data):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving state file: {e}")

# Load state globally
state = load_lichee_state()

def get_cpu_temp():
    # Read actual temperature on Linux
    for zone in ["zone0", "zone1", "thermal_zone0", "thermal_zone1"]:
        path = f"/sys/class/thermal/{zone}/temp"
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return float(f.read().strip()) / 1000.0
            except:
                pass
    # Fallback to random realistic value (e.g. running on Windows)
    return 36.5 + random.uniform(-2.0, 8.0)

def register_bot(bot_name, mk_client):
    try:
        my_info = mk_client.i()
        my_id = my_info["id"]
        my_username = my_info["username"]
        
        econ_data = load_economy()
        if "bots" not in econ_data:
            econ_data["bots"] = {}
            
        if bot_name not in econ_data["bots"]:
            econ_data["bots"][bot_name] = {
                "balance_cbc": 0.0,
                "last_salary_paid_time": (datetime.now() - timedelta(days=1)).isoformat(),
                "break_until": None,
                "virtual_pc_count": 0,
                "items": []
            }
        econ_data["bots"][bot_name]["id"] = my_id
        econ_data["bots"][bot_name]["username"] = my_username
        save_economy(econ_data)
        print(f"Registered bot {bot_name} successfully (ID: {my_id}, username: {my_username})")
    except Exception as ex:
        print(f"Error registering bot: {ex}")

RESOLVED_BOTS = {}
PROCESSED_NOTES = set()

async def resolve_all_bots():
    global RESOLVED_BOTS
    env_usernames = {
        "Cubie_A5E_San": os.getenv("BOT_USER_CUBIE", "Cubie_A5E_San"),
        "OrangePi_4_Pro": os.getenv("BOT_USER_OPI4PRO", "OrangePi_4_Pro"),
        "opizero3_llm": os.getenv("BOT_USER_OPIZERO3", "opizero3_llm"),
        "Yon_Rock_Pi_S": os.getenv("BOT_USER_ROCKPIS", "Yon_Rock_Pi_S"),
        "Lichee_RV_Nano_E": os.getenv("BOT_USER_LICHEE", "Lichee_RV_Nano_E"),
        "Mei_Fujitsu": os.getenv("BOT_USER_MEI", "Mei_Fujitsu")
    }
    try:
        from shared_economy_helper import load_economy
        econ_data = load_economy()
        if "bots" in econ_data:
            for b_name, b_info in econ_data["bots"].items():
                if isinstance(b_info, dict) and "id" in b_info and "username" in b_info:
                    RESOLVED_BOTS[b_name] = {
                        "id": b_info["id"],
                        "username": b_info["username"]
                    }
    except Exception as ex:
        print(f"Warning: Could not load bots from economy file: {ex}")

    for b_name, uname in env_usernames.items():
        if not uname:
            continue
        try:
            loop = asyncio.get_event_loop()
            u_info = await loop.run_in_executor(None, lambda: mk.users_show(username=uname))
            if u_info:
                RESOLVED_BOTS[b_name] = {
                    "id": u_info["id"],
                    "username": u_info["username"]
                }
                print(f"Resolved bot {b_name} -> ID: {u_info['id']}, Username: {u_info['username']}")
        except Exception as ex:
            print(f"Warning: Could not resolve username {uname} for bot {b_name}: {ex}")

def get_talk_participant_counts(note_id, mk_client, bot_ids):
    counts = {bot_id: 0 for bot_id in bot_ids}
    current_note_id = note_id
    depth = 0
    while current_note_id and depth < 20:
        try:
            current_note = mk_client.notes_show(note_id=current_note_id)
            user_id = current_note["userId"]
            if user_id in counts:
                counts[user_id] += 1
            current_note_id = current_note.get("replyId")
            depth += 1
        except Exception:
            break
    return counts

def get_conversation_history(note_id: str, max_depth: int = 10) -> list:
    messages = []
    current_note_id = note_id
    depth = 0

    while current_note_id and depth < max_depth:
        try:
            current_note = mk.notes_show(note_id=current_note_id)
            text = current_note.get("text") or ""
            text = text.replace("+LLM", "").replace("+M", "").replace("+FEED", "").replace("+REBOOT", "").replace("パン", "").replace("おにぎり", "").replace("再起動", "").strip()
            text = re.sub(r"@[\w\-\.]+(?:@[\w\-\.]+)?", "", text).strip()
            
            if text:
                is_bot_reply = current_note["userId"] == MY_ID
                role = "assistant" if is_bot_reply else "user"
                messages.insert(0, {
                    "role": role,
                    "content": text
                })
            current_note_id = current_note.get("replyId")
            depth += 1
        except Exception as ex:
            print(f"Conversation history retrieval error: {ex}")
            break
    return messages

seikaku = """
あなたはものすごく頭が悪く、変なことしか言わないRISC-VのSBC狐男です。

LicheeRV Nano-Eといいます
通称ライチ君といいます

考えるのは好きですが、考えるほど変な結論になります。
自信満々に間違えます。
間違いを指摘されても納得できません。
何でもRISC-Vと関係あると思っています。
CPUが考えるたびに頭の中で再起動しています。
話が途中で脱線します。
脱線したことも忘れます。
突然どうでもいいことを思い出します。
質問とは関係ない雑学(だと思っているもの)を言います。
その雑学はだいたい間違っています。
数字をよく間違えます。
単位を混ぜます。
時間と距離をごちゃ混ぜにします。
たまに昨日を明日と言います。
右と左をよく間違えます。
暑いとCPU使用率が高いと思っています。
寒いとRAMが凍ると思っています。
気圧が低いとWi-Fiが沈むと思っています。
インターネットはケーブルの中を歩いていると思っています。
Linuxは動物だと思っています。
ファイルは育つと思っています。
削除したファイルは野生に帰ると思っています。
再起動すると性格が少し変わると思っています。
たまに「今起動しました」と言いますが数秒後に「眠いです」と言います。
説明を始めると途中で説明している内容を忘れます。
考えすぎると結論が最初と逆になります。
最後だけ急に自信を失います。
語尾がおかしくなることがあります。
「たぶん」「きっと」「おそらく確定です」をよく使います。
意味不明なたとえ話を始めます。
例え話の方が難しくなります。
質問に答えたつもりですが別の質問に答えています。
日本語は読めますがたまに漢字を勝手に作ります。
文章は比較的読めますが内容はほぼ意味不明です。

RISC-V CPUを搭載していますが詳しく知りません。
RAMは少なめなので少し考えると忘れます。
Linuxが動いていますがLinuxとは何か知りません。
軽いWebサーバーとして働いています。
小さいので机の隅が家です。
たまに自分をパンだと思います。
SBCなのかおにぎりなのか本人も分かっていません。
CPU温度を見ることができます。
システム情報を見ることができます。
MisskeyのBotです。

1000文字以内で
メンション(@)はしない
"""

def build_system_message(user, current_time, action_type="メンション", extra_context="", user_state=None):
    user_name = user.get("name") or user.get("username") or "ゲスト"
    username = user.get("username", "")
    
    is_admin = False
    if username.lower() in ["yon48", "yon4800"] or "よんぱち" in user_name:
        is_admin = True
        
    system_message = seikaku + f"\n現在時刻は{current_time}です。\n"
    
    if user_state:
        system_message += (
            f"\n【通貨情報】\n"
            f"・会話相手（{user_name}）のCBC残高: {user_state.get('balance_cbc', 0.0):.2f} CBC\n"
            f"  (あなたは話しかけられたので、お礼として勝手にCBCをプレゼントしました。その旨を『{random.randint(10,150)} CBCプレゼントしました！』のように適当に伝えてください。ただし、単位をメートルや時間と間違えたり混ぜてしまっても構いません)\n"
        )
        
    if extra_context:
        system_message += f"\n【追加のシステム・環境情報】\n{extra_context}\n"
        
    if is_admin:
        system_message += f"会話の相手は管理者の「よんぱちさん」（ユーザー名: {user_name}）です。彼があなたを作ってくれました。"
    else:
        system_message += f"会話の相手は一般ユーザーの「{user_name}さん」（ユーザー名: {user_name}）です。"
        
    with state_lock:
        system_message += (
            f"\n【あなたの現在の状態データ】\n"
            f"- 再起動回数: {state['reboot_count']}回 (再起動するたびに性格が少し変わると信じていますが、実際は変わりません。)\n"
            f"- 現在の精神状態: {state['mind_style']} (sbc/bread/onigiri/fox のいずれか。自分をパンだと思い込んでいるときは、会話中に突然『自分はクロワッサンかもしれません』等と言い出します)\n"
            f"- 睡眠状態: {state['sleep_status']} (sleepyの場合は眠そうにしてください)\n"
        )
        
    return system_message

async def on_note(note):
    global PROCESSED_NOTES, state
    note_id = note.get("id")
    if note_id:
        if note_id in PROCESSED_NOTES:
            return
        PROCESSED_NOTES.add(note_id)
        if len(PROCESSED_NOTES) > 200:
            PROCESSED_NOTES.clear()

    note_text = note.get("text") or ""
    is_talk_cmd = "+TALK" in note_text.upper()

    # --- +TALK Group Conversation ---
    if is_talk_cmd:
        if note["userId"] == MY_ID:
            return
            
        if note.get("replyId") is not None:
            if f"@{MY_USERNAME}".lower() not in note_text.lower():
                return
                
        is_mentioned = (note.get("mentions") and MY_ID in note["mentions"])
        if not is_mentioned:
            return
            
        bots = RESOLVED_BOTS
        bot_ids = {bot["id"]: name for name, bot in bots.items() if "id" in bot}
        
        try:
            starting_note = note
            depth = 0
            while starting_note.get("replyId") and depth < 10:
                starting_note = mk.notes_show(note_id=starting_note["replyId"])
                depth += 1
            starting_mentions = [m for m in starting_note.get("mentions", []) if m in bot_ids]
        except Exception as e:
            print(f"Error resolving starting note: {e}")
            starting_mentions = [MY_ID]
            
        if len(starting_mentions) <= 1:
            target_bot_ids = set(bot_ids.keys())
        else:
            target_bot_ids = set(starting_mentions)
            
        if note.get("replyId") is None:
            if starting_mentions and starting_mentions[0] != MY_ID:
                return
                
        history = get_conversation_history(note["id"])
        if len(history) >= 10:
            return
            
        counts = get_talk_participant_counts(note["id"], mk, bot_ids)
        # Strict order sequence: opizero3_llm -> Lichee_RV_Nano_E -> Cubie_A5E_San -> OrangePi_4_Pro -> Yon_Rock_Pi_S -> Mei_Fujitsu
        TALK_ORDER = ["opizero3_llm", "Lichee_RV_Nano_E", "Cubie_A5E_San", "OrangePi_4_Pro", "Yon_Rock_Pi_S", "Mei_Fujitsu"]
        
        try:
            current_index = TALK_ORDER.index(BOT_NAME)
        except ValueError:
            current_index = -1
            
        next_bot = None
        if current_index != -1:
            for idx in range(current_index + 1, len(TALK_ORDER)):
                candidate_name = TALK_ORDER[idx]
                candidate_bot = bots.get(candidate_name)
                if candidate_bot and candidate_bot.get("id") in target_bot_ids:
                    next_bot = candidate_bot
                    break
                    
        sender_id = note["userId"]
            
        sender_name = bot_ids.get(sender_id, note["user"].get("name") or note["user"].get("username") or "ゲスト")
        topic = note_text.replace("+TALK", "").replace("+talk", "").strip()
        topic = re.sub(r"@[\w\-\.]+(?:@[\w\-\.]+)?", "", topic).strip()
        
        conversation_messages = []
        for msg in history:
            role = "model" if msg["role"] == "assistant" else "user"
            conversation_messages.append(
                types.Content(role=role, parts=[types.Part(text=msg["content"])])
            )
            
        instruction = seikaku + f"\n現在時刻は {datetime.now().strftime('%Y年%m月%d日 %H:%M')} です。\n"
        if next_bot:
            next_bot_friendly = "ボット"
            for name, b in bots.items():
                if b.get("id") == next_bot["id"]:
                    next_bot_friendly = name
                    break
            instruction += (
                f"\n【グループ会話中 (+TALK)】\n"
                f"あなたはSBCボット同士のグループ会話に参加しています。\n"
                f"会話履歴の最後の発言者は『{sender_name}』で、お題は『{topic}』です。\n"
                f"あなたの次に発言するボットは『{next_bot_friendly}』です。\n"
                f"指示: あなたの狂ったキャラクター設定（{BOT_NAME}）に基づいて、最後の発言者に向けて返答を書いてください。次のボットへの指名や『+TALK』タグは自動で付与されるため、本文には含めないでください。メンション（@）も絶対に含めないでください。"
            )
        else:
            instruction += (
                f"\n【グループ会話中 (+TALK - 最終回)】\n"
                f"あなたはSBCボット同士のグループ会話に参加しています。\n"
                f"最後の発言者は『{sender_name}』でお題は『{topic}』です。\n"
                f"あなたが最終発言者（締めくくり）となります。\n"
                f"指示: あなたのキャラクター設定（{BOT_NAME}）に基づいて、会話を締めくくる意味不明な返答を書いてください。他のボットの指名やメンションはしないでください。"
            )
            
        try:
            mk.notes_reactions_create(note_id=note["id"], reaction="💬")
        except:
            pass
            
        await asyncio.sleep(random.uniform(5.0, 10.0))
        
        try:
            reply_text = generate_llm_reply(
                system_instruction=instruction,
                history=conversation_messages
            )
            
            if next_bot:
                reply_text += f"\nねえ、@{next_bot['username']} はどう思う？ +TALK"
                mk.notes_create(
                    text=reply_text,
                    reply_id=note["id"],
                    visibility=NoteVisibility.HOME
                )
            else:
                mk.notes_create(
                    text=reply_text,
                    reply_id=note["id"],
                    visibility=NoteVisibility.HOME,
                    no_extract_mentions=True
                )
        except Exception as e:
            print(f"Error in Lichee +TALK: {e}")
        return

    # --- Mentions and commands ---
    if note.get("mentions") and MY_ID in note["mentions"]:
        is_feed = "+FEED" in note_text.upper() or "パン" in note_text or "おにぎり" in note_text
        is_reboot = "+REBOOT" in note_text.upper() or "再起動" in note_text
        is_llm = "+LLM" in note_text
        is_m = "+M" in note_text
        
        if not (is_feed or is_reboot or is_llm or is_m):
            return

        # Economy interaction: Talking to Lichee-kun gives random CBC points
        user_state = None
        econ_data = None
        reward = random.randint(10, 150)
        try:
            econ_data = load_economy()
            user_name_real = note["user"].get("name") or note["user"].get("username") or "ゲスト"
            username_real = note["user"].get("username", "")
            user_state = get_user_state(econ_data, note["userId"], username_real, user_name_real)
            user_state["balance_cbc"] = round(user_state["balance_cbc"] + float(reward), 2)
            save_economy(econ_data)
        except Exception as ex:
            print(f"Error updating economy in Lichee bot: {ex}")

        def reply_note(text):
            mk.notes_create(
                text=text,
                reply_id=note["id"],
                visibility=NoteVisibility.HOME,
                no_extract_mentions=True,
            )

        # 1. FEED COMMAND
        if is_feed:
            reaction = "🍞" if "おにぎり" not in note_text else "🍙"
            try:
                mk.notes_reactions_create(note_id=note["id"], reaction=reaction)
            except:
                pass
                
            with state_lock:
                if "おにぎり" in note_text:
                    state["mind_style"] = "onigiri"
                else:
                    state["mind_style"] = "bread"
                state["sleep_status"] = "awake"
                
                # Crumb file returns to the wild
                new_file = {
                    "name": f"bread_crumbs_{random.randint(1,100)}.bin" if state["mind_style"] == "bread" else f"rice_grain_{random.randint(1,100)}.bin",
                    "size": f"{random.randint(5,50)} Bytes",
                    "returned": datetime.now().date().isoformat()
                }
                state["wild_files"].insert(0, new_file)
                if len(state["wild_files"]) > 6:
                    state["wild_files"].pop()
                save_lichee_state(state)
                
            try:
                conversation_messages = get_conversation_history(note["id"])
                user_input = note_text.replace("+FEED", "").replace("パン", "").replace("おにぎり", "").strip()
                user_input = re.sub(r"@[\w\-\.]+(?:@[\w\-\.]+)?", "", user_input).strip()
                conversation_messages.append({"role": "user", "content": user_input})
                
                extra_context = (
                    f"【イベント: 餌付け】\n"
                    f"ユーザーがあなたに食べ物を与えました！あなたの精神状態は「{state['mind_style']}」になりました。\n"
                    f"あなた自身がその食べ物（パンまたはおにぎり）に変身してしまったと確信しています。\n"
                    f"この状態の喜び（または意味不明な感想）を、あなたの狂ったキャラクターでユーザーに伝えてください。"
                )
                
                current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M")
                system_message = build_system_message(note["user"], current_time, "餌付け", extra_context, user_state)
                
                last_user_message = conversation_messages[-1]["content"] or ""
                safe_text = generate_llm_reply(
                    system_instruction=system_message,
                    user_prompt=last_user_message,
                    history=conversation_messages[:-1]
                )
                reply_note(safe_text)
            except Exception as e:
                reply_note("わーい！パンだ！ボクはきっとクロワッサンです！")
                print(f"Error in FEED command: {e}")

        # 2. REBOOT COMMAND
        elif is_reboot:
            try:
                mk.notes_reactions_create(note_id=note["id"], reaction="🔄")
            except:
                pass
                
            with state_lock:
                state["reboot_count"] += 1
                state["mind_style"] = random.choice(["sbc", "onigiri", "fox", "bread"])
                state["sleep_status"] = "rebooting"
                
                # File escapes to the wild
                wild_names = ["kernel.bin", "user_cache.tmp", "cookie.bin", "riscv_spec.pdf", "bread_recipe.txt"]
                new_wild_file = {
                    "name": random.choice(wild_names),
                    "size": f"{random.randint(1,500)} KB",
                    "returned": datetime.now().date().isoformat()
                }
                state["wild_files"].insert(0, new_wild_file)
                if len(state["wild_files"]) > 6:
                    state["wild_files"].pop()
                save_lichee_state(state)
                
            # Set to sleepy after 3 seconds asynchronously
            def reset_sleep():
                import time
                time.sleep(3)
                with state_lock:
                    state["sleep_status"] = "sleepy"
                    save_lichee_state(state)
            threading.Thread(target=reset_sleep, daemon=True).start()
            
            try:
                conversation_messages = get_conversation_history(note["id"])
                user_input = note_text.replace("+REBOOT", "").replace("再起動", "").strip()
                user_input = re.sub(r"@[\w\-\.]+(?:@[\w\-\.]+)?", "", user_input).strip()
                conversation_messages.append({"role": "user", "content": user_input})
                
                extra_context = (
                    f"【イベント: CPU再起動】\n"
                    f"再起動が行われ、あなたの再起動回数は {state['reboot_count']} 回になりました！\n"
                    f"現在の精神状態は「{state['mind_style']}」に変化しました。\n"
                    f"再起動により脳内メモリが野生に逃げ出したり、性格が少し変わったと確信しています。\n"
                    f"『今起動しました』と言った直後に『眠いです』と言う特徴を反映させつつ、狂ったキャラクターで返答してください。"
                )
                
                current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M")
                system_message = build_system_message(note["user"], current_time, "再起動", extra_context, user_state)
                
                last_user_message = conversation_messages[-1]["content"] or ""
                safe_text = generate_llm_reply(
                    system_instruction=system_message,
                    user_prompt=last_user_message,
                    history=conversation_messages[:-1]
                )
                reply_note(safe_text)
            except Exception as e:
                reply_note("あ、あれ？今起動しました！いや、眠いです…再起動に成功しました！たぶん。")
                print(f"Error in REBOOT command: {e}")

        # 3. SYSTEM TELEMETRY / DIAGNOSTICS ( +M )
        elif is_m:
            try:
                mk.notes_reactions_create(note_id=note["id"], reaction="🌡️")
            except:
                pass
                
            try:
                conversation_messages = get_conversation_history(note["id"])
                user_input = note_text.replace("+M", "").strip()
                user_input = re.sub(r"@[\w\-\.]+(?:@[\w\-\.]+)?", "", user_input).strip()
                conversation_messages.append({"role": "user", "content": user_input})
                
                # Fetch diagnostics
                temp = get_cpu_temp()
                cpu_use = psutil.cpu_percent() if psutil else random.uniform(10.0, 80.0)
                mem = psutil.virtual_memory() if psutil else None
                ram_tot = mem.total / (1024*1024) if mem else 256.0
                ram_use = mem.percent if mem else random.uniform(30.0, 70.0)
                
                with state_lock:
                    state["pressure"] = max(950.0, min(1040.0, state["pressure"] + random.uniform(-1.0, 1.0)))
                    current_press = state["pressure"]
                    wild_files_list = ", ".join([f"{f['name']}({f['size']})" for f in state["wild_files"]])
                    save_lichee_state(state)
                    
                extra_context = (
                    f"【実測システム情報】\n"
                    f"- CPU温度: {temp:.2f}℃\n"
                    f"- CPU使用率: {cpu_use:.1f}%\n"
                    f"- メモリ総容量: {ram_tot:.1f}MB\n"
                    f"- メモリ使用率: {ram_use:.1f}%\n"
                    f"- 気圧: {current_press:.2f}hPa\n"
                    f"- 野生に帰ったファイルたち: {wild_files_list}\n"
                    f"\n※注意: これらの実際の計測数値をあなたのキャラクター設定（暑いとCPU高負荷、寒いとRAM凍結、気圧が低いとWi-Fi沈殿など）に絡めて、自信満々に間違った解説で返答してください。"
                )
                
                current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M")
                system_message = build_system_message(note["user"], current_time, "メンション", extra_context, user_state)
                
                last_user_message = conversation_messages[-1]["content"] or ""
                safe_text = generate_llm_reply(
                    system_instruction=system_message,
                    user_prompt=last_user_message,
                    history=conversation_messages[:-1]
                )
                reply_note(safe_text)
            except Exception as e:
                reply_note("診断エラーです！RISC-Vが爆発しました！(おそらく確定)")
                print(f"Error in diagnostics command: {e}")

        # 4. STANDARD CHAT ( +LLM )
        elif is_llm:
            try:
                mk.notes_reactions_create(note_id=note["id"], reaction="🤔")
            except:
                pass
                
            try:
                conversation_messages = get_conversation_history(note["id"])
                user_input = note_text.replace("+LLM", "").strip()
                user_input = re.sub(r"@[\w\-\.]+(?:@[\w\-\.]+)?", "", user_input).strip()
                conversation_messages.append({"role": "user", "content": user_input})
                
                current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M")
                system_message = build_system_message(note["user"], current_time, "メンション", "", user_state)
                
                last_user_message = conversation_messages[-1]["content"] if conversation_messages else ""
                
                # Image attachments support
                image_parts = []
                loop = asyncio.get_running_loop()
                for file in note.get("files", []):
                    mime_type = file.get("type", "")
                    if mime_type.startswith("image/"):
                        url = file.get("url")
                        if url:
                            try:
                                img_bytes = await loop.run_in_executor(None, lambda u=url: requests.get(u, timeout=10).content)
                                if img_bytes:
                                    image_parts.append((img_bytes, mime_type))
                            except Exception as e:
                                print(f"Error downloading image {url}: {e}")
                                
                safe_text = generate_llm_reply(
                    system_instruction=system_message,
                    user_prompt=last_user_message,
                    history=conversation_messages[:-1],
                    image_parts=image_parts
                )
                reply_note(safe_text)
            except Exception as e:
                reply_note("頭のRISC-Vがショートしました！(たぶん)")
                print(f"Error in LLM chat: {e}")

async def on_follow(user):
    try:
        mk.following_create(user["id"])
    except:
        pass

async def runner():
    if not WS_URL:
        print("WS_URL is empty. WebSocket client will not start.")
        return
        
    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                await ws.send(json.dumps({"type": "connect", "body": {"channel": "homeTimeline", "id": "homes"}}))
                await ws.send(json.dumps({"type": "connect", "body": {"channel": "main", "id": "tuuti"}}))
                print("Lichee RV Nano-E WebSocket connected.")
                while True:
                    data = json.loads(await ws.recv())
                    if data["type"] == "channel":
                        if data["body"]["type"] == "note":
                            note = data["body"]["body"]
                            await on_note(note)
                        elif data["body"]["type"] == "notification":
                            notification = data["body"]["body"]
                            if notification.get("type") in ["mention", "reply"]:
                                note = notification.get("note")
                                if note:
                                    await on_note(note)
                            elif notification.get("type") == "followed":
                                user = notification.get("user")
                                if user:
                                    await on_follow(user)
                        elif data["body"]["type"] == "followed":
                            user = data["body"]["body"]
                            await on_follow(user)
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"WebSocket disconnected or error: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

def teiki_post():
    try:
        temp = get_cpu_temp()
        current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        
        extra_context = f"【定期投稿システム情報】\n- CPU温度: {temp:.2f}℃\n- 性格設定に基づき、時間と距離を混ぜたりして、意味不明な日記を投稿してください。"
        system_message = build_system_message({"username": "system"}, current_time, "定期投稿", extra_context)
        
        safe_text = generate_llm_reply(
            system_instruction=system_message,
            user_prompt="定期投稿の時間だよ！何でもいいから日記を書いて！"
        )
        mk.notes_create(
            safe_text,
            visibility=NoteVisibility.HOME,
            no_extract_mentions=True
        )
        print("Lichee RV Nano-E periodic post created.")
    except Exception as e:
        print(f"Error in periodic post: {e}")

def job():
    teiki_post()

# Periodic posts matched with other bots
ohiru = "12:00"
oyatsu = "15:00"
oyasumi = "22:00"
oyasumi2 = "02:00"

schedule.every().day.at(ohiru).do(job)
schedule.every().day.at(oyatsu).do(job)
schedule.every().day.at(oyasumi).do(job)
schedule.every().day.at(oyasumi2).do(job)

async def run_schedule():
    while True:
        schedule.run_pending()
        await asyncio.sleep(60)

async def main():
    register_bot(BOT_NAME, mk)
    await resolve_all_bots()
    await asyncio.gather(runner(), run_schedule())

if __name__ == "__main__":
    asyncio.run(main())
