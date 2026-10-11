"""在测试 Pod 里做端到端验证（模拟报文，不需要真秤）。

用法: python3 -I tests/e2e_pod.py <命令>
  reset      删掉现有配置条目和存储，等秤被蓝牙重新发现后添加，并开启调试（从零开始）
  fresh      从零用户开始：第一次称重弹卡片 → 新建 小明；陌生人 → 新建 小红
  run        归属、不重复提交、换人、访客、手动归属与撤销、错误处理
  discovery  发现卡片：替换、撤销、菜单两条路径（新建 / 归给现有并重置基准）
  zero       删到 0 个用户后，第一次称重仍然弹卡片
  state      只打印当前各实体状态
需要 Pod 在 http://172.18.1.12:8123，账号 test/test。
"""

import asyncio
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import aiohttp

B = "http://172.18.1.12:8123"
CID = B + "/"
TOKEN = None

X, R, G = "AFU 体脂秤 小明 ", "AFU 体脂秤 小红 ", "AFU 体脂秤 小刚 "
L, C = "AFU 体脂秤 最近测量归属", "AFU 体脂秤 归属置信度"


# ---- 基础 ----------------------------------------------------------------

def req(method, path, data=None, form=False):
    h = {}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    try:
        r = urllib.request.urlopen(urllib.request.Request(B + path, body, h, method=method))
        raw = r.read()
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:500]}


def login():
    global TOKEN
    f = req("POST", "/auth/login_flow", {"client_id": CID, "handler": ["homeassistant", None], "redirect_uri": CID + "?auth_callback=1"})
    r = req("POST", "/auth/login_flow/" + f["flow_id"], {"client_id": CID, "username": "test", "password": "test"})
    TOKEN = req("POST", "/auth/token", {"client_id": CID, "grant_type": "authorization_code", "code": r["result"]}, form=True)["access_token"]


def entry_id(required=True):
    for e in req("GET", "/api/config/config_entries/entry"):
        if e["domain"] == "afu_scale":
            return e["entry_id"]
    if required:
        raise SystemExit("没有 afu_scale 的配置条目（先运行 reset）")
    return None


def options_step(eid, step, data=None):
    """走一遍选项流：菜单 → 选 step →（可选）提交表单。"""
    f = req("POST", "/api/config/config_entries/options/flow", {"handler": eid})
    f = req("POST", f"/api/config/config_entries/options/flow/{f['flow_id']}", {"next_step_id": step})
    if data is not None:
        f = req("POST", f"/api/config/config_entries/options/flow/{f['flow_id']}", data)
    return f


def pkt(kg, imp, stable=True, battery=0x64):
    g = int(round(kg * 1000))
    d = bytearray(20)
    d[0], d[1], d[2] = 0xAC, 0x29, 0x80
    d[3], d[4], d[5] = 0x68 + (g >> 16), (g >> 8) & 0xFF, g & 0xFF
    d[6] = 2 if stable else 1
    d[7] = battery  # mode 1 的电量位置
    d[8], d[9] = imp >> 8, imp & 0xFF
    d[11] = battery  # mode 2 的电量位置
    return d.hex()


def inject(kg, imp, stable=True, battery=0x64):
    r = req("POST", "/api/services/afu_scale/inject_packet", {"packet": pkt(kg, imp, stable, battery)})
    if isinstance(r, dict) and "_http_error" in r:
        raise SystemExit(f"inject 失败: {r}")


def states():
    out = {}
    for e in req("GET", "/api/states"):
        n = e["attributes"].get("friendly_name", "")
        if n.startswith("AFU 体脂秤"):
            out[n] = e["state"]
    return out


def wf(s, key):
    return float(s[key]) if s.get(key) not in (None, "unknown", "unavailable") else None


def settle(sec=1.0):
    time.sleep(sec)
    return states()


def wait_idle():
    """等"测量中"过期（15 秒），保证下一次称重会被当作新的一次提交。"""
    time.sleep(16)


failures = 0


def check(label, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
    failures += 0 if cond else 1


# ---- 发现流程 ------------------------------------------------------------

def progress(source):
    async def _p():
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(B.replace("http", "ws") + "/api/websocket") as ws:
                await ws.receive_json()
                await ws.send_json({"type": "auth", "access_token": TOKEN})
                await ws.receive_json()
                await ws.send_json({"id": 1, "type": "config_entries/flow/progress"})
                return (await ws.receive_json())["result"]
    return [
        f for f in asyncio.run(_p())
        if f.get("handler") == "afu_scale" and f.get("context", {}).get("source") == source
    ]


def cards():
    """当前未处理的"新用户"发现卡片。"""
    return progress("integration_discovery")


def card_step(fid, step=None, data=None):
    """推进一张卡片：先可选地在菜单里选 step，再可选地提交表单。"""
    url = f"/api/config/config_entries/flow/{fid}"
    f = req("POST", url, {"next_step_id": step}) if step else req("GET", url)
    if data is not None:
        f = req("POST", url, data)
    return f


def one_card(label):
    cs = cards()
    check(label, len(cs) == 1, str(len(cs)))
    return cs[0] if cs else None


# ---- reset / fresh --------------------------------------------------------

def reset():
    eid = entry_id(required=False)
    if eid:
        print("删除条目", eid, req("DELETE", f"/api/config/config_entries/entry/{eid}"))
        subprocess.run(
            ["ssh", "tony@172.18.1.12", f"sudo rm -f /home/tony/ha-afu-test/config/.storage/afu_scale.{eid}"],
            check=False,
        )
    print("等待秤被蓝牙重新发现（秤要在广播）…")
    for _ in range(60):
        fl = progress("bluetooth")
        if fl:
            r = req("POST", f"/api/config/config_entries/flow/{fl[0]['flow_id']}", {})
            print("添加条目:", r.get("type"), r.get("title"))
            break
        time.sleep(3)
    else:
        raise SystemExit("没等到蓝牙发现；改用手动流程添加")
    time.sleep(4)
    print("开启调试:", options_step(entry_id(), "debug", {"debug": True}).get("type"))
    time.sleep(4)


def fresh():
    print("\n[F1] 没有任何用户：第一次称重 70.0kg → 访客 + 卡片")
    s = states()
    check("起始没有用户实体", not any(k.endswith(" 体重") and k != "AFU 体脂秤 体重" for k in s))
    wait_idle()
    inject(70.0, 520)
    s = settle(2)
    check("归属=访客", s.get(L) == "访客", s.get(L))
    card = one_card("弹出一张卡片")
    if card:
        step = card_step(card["flow_id"])
        check("没有用户时直接是新建表单（没有菜单）", step.get("step_id") == "new_user", str(step.get("step_id")))
        done = card_step(card["flow_id"], data={"name": "小明", "height_cm": 175, "sex": "male", "age": 30})
        check("流程以 user_added 结束", done.get("reason") == "user_added", str(done.get("reason")))
    time.sleep(8)
    s = states()
    check("小明 体重=70.0（那次读数被采纳）", wf(s, X + "体重") == 70.0, s.get(X + "体重"))
    check("归属=小明", s.get(L) == "小明", s.get(L))

    print("\n[F2] 已有小明，来了个 55.0kg 的陌生人 → 卡片 → 菜单 → 新建 小红")
    wait_idle()
    inject(55.0, 640)
    s = settle(2)
    check("归属=访客（没有被算成小明）", s.get(L) == "访客", s.get(L))
    check("小明 体重 没被污染", wf(s, X + "体重") == 70.0, s.get(X + "体重"))
    card = one_card("弹出一张卡片")
    if card:
        step = card_step(card["flow_id"])
        check("有用户时先出菜单", step.get("type") == "menu" and set(step.get("menu_options", [])) == {"new_user", "assign_existing"}, f"{step.get('type')} {step.get('menu_options')}")
        card_step(card["flow_id"], step="new_user")
        done = card_step(card["flow_id"], data={"name": "小红", "height_cm": 160, "sex": "female", "age": 28})
        check("流程以 user_added 结束", done.get("reason") == "user_added", str(done.get("reason")))
    time.sleep(8)
    s = states()
    check("小红 体重=55.0", wf(s, R + "体重") == 55.0, s.get(R + "体重"))
    check("小明 体重 仍为 70.0", wf(s, X + "体重") == 70.0, s.get(X + "体重"))

    print("\n[F3] 之后各自再称 → 直接认出，不再弹卡片")
    wait_idle()
    inject(70.4, 525)
    s = settle(2)
    check("70.4 → 小明 high", s.get(L) == "小明" and s.get(C) == "high", f"{s.get(L)}/{s.get(C)}")
    wait_idle()
    inject(55.3, 645)
    s = settle(2)
    check("55.3 → 小红 high", s.get(L) == "小红" and s.get(C) == "high", f"{s.get(L)}/{s.get(C)}")
    check("没有卡片", len(cards()) == 0, str(len(cards())))
    print(f"\n失败: {failures}")
    return failures


# ---- run: 归属 / 提交 / 访客 / 手动归属 -----------------------------------------

def run():
    s0 = states()  # 实体会恢复上次的值，所以断言"没变"而不是"为空"
    x0, r0 = s0.get(X + "体重"), s0.get(R + "体重")
    wait_idle()

    print("\n[1] 不稳定读数：只更新实时体重，不归属")
    inject(70.1, 520, stable=False)
    s = settle()
    check("实时体重更新", wf(s, "AFU 体脂秤 体重") == 70.1, s.get("AFU 体脂秤 体重"))
    check("小明 体重 没变", s.get(X + "体重") == x0, f"{s.get(X + '体重')} vs {x0}")

    print("\n[2] 稳定读数 70.2kg → 小明")
    inject(70.2, 520)
    s = settle()
    check("归属=小明, high", s.get(L) == "小明" and s.get(C) == "high", f"{s.get(L)}/{s.get(C)}")
    check("小明 体重 70.2", wf(s, X + "体重") == 70.2, s.get(X + "体重"))
    check("小明 体脂率有值", wf(s, X + "体脂率") is not None, s.get(X + "体脂率"))
    check("小红 体重 没变", s.get(R + "体重") == r0, f"{s.get(R + '体重')} vs {r0}")
    check("电量 100", wf(s, "AFU 体脂秤 电量") == 100, s.get("AFU 体脂秤 电量"))

    print("\n[3] 同一次称重里的第二个稳定包：不重复提交")
    inject(70.5, 525)
    s = settle()
    check("小明 体重 仍为 70.2", wf(s, X + "体重") == 70.2, s.get(X + "体重"))

    print("\n[4] 等 16 秒（测量结束）后 55.4kg → 小红")
    wait_idle()
    inject(55.4, 640)
    s = settle()
    check("归属=小红", s.get(L) == "小红", s.get(L))
    check("小红 体重 55.4", wf(s, R + "体重") == 55.4, s.get(R + "体重"))
    check("小明 体重 不受影响", wf(s, X + "体重") == 70.2, s.get(X + "体重"))

    print("\n[5] 同一次会话里换人（体重差 > 3kg 允许再提交）：70.0kg → 小明")
    inject(70.0, 515)
    s = settle()
    check("归属=小明, 体重 70.0", s.get(L) == "小明" and wf(s, X + "体重") == 70.0, f"{s.get(L)} {s.get(X + '体重')}")

    print("\n[6] 62kg 不在任何人范围内 → 访客")
    inject(62.0, 560)
    s = settle()
    check("归属=访客 guest", s.get(L) == "访客" and s.get(C) == "guest", f"{s.get(L)}/{s.get(C)}")
    check("小明/小红 体重 不变", wf(s, X + "体重") == 70.0 and wf(s, R + "体重") == 55.4, f"{s.get(X + '体重')}/{s.get(R + '体重')}")

    print("\n[7] 把访客读数归给小红（用服务）")
    r = req("POST", "/api/services/afu_scale/assign_last_measurement", {"user": "小红"})
    check("服务调用成功", not (isinstance(r, dict) and "_http_error" in r), str(r)[:120])
    s = settle()
    check("归属=小红, manual", s.get(L) == "小红" and s.get(C) == "manual", f"{s.get(L)}/{s.get(C)}")
    check("小红 体重 62.0", wf(s, R + "体重") == 62.0, s.get(R + "体重"))
    check("手动归属后卡片被撤掉", len(cards()) == 0, str(len(cards())))

    print("\n[8] 再改归属给小明：小红应恢复为 55.4")
    req("POST", "/api/services/afu_scale/assign_last_measurement", {"user": "小明"})
    s = settle()
    check("归属=小明, 体重 62.0", s.get(L) == "小明" and wf(s, X + "体重") == 62.0, f"{s.get(L)} {s.get(X + '体重')}")
    check("小红 体重 恢复 55.4", wf(s, R + "体重") == 55.4, s.get(R + "体重"))

    print("\n[9] 错误处理")
    r = req("POST", "/api/services/afu_scale/assign_last_measurement", {"user": "不存在的人"})
    check("未知用户被拒绝", isinstance(r, dict) and "_http_error" in r, str(r)[:100])
    r = req("POST", "/api/services/afu_scale/inject_packet", {"packet": "zz"})
    check("非法十六进制被拒绝", isinstance(r, dict) and "_http_error" in r, str(r)[:100])

    print(f"\n失败: {failures}")
    return failures


# ---- discovery -----------------------------------------------------------

def run_discovery():
    s = states()
    x0 = wf(s, X + "体重")
    wait_idle()

    print("\n[D1] 访客读数 63.0kg → 一张卡片；再来 63.5kg → 被替换，仍只有一张")
    inject(63.0, 570)
    time.sleep(2)
    c1 = one_card("出现一张卡片")
    if c1:
        check("标题带体重 63.0", "63.0" in str(c1["context"].get("title_placeholders")), str(c1["context"].get("title_placeholders")))
    wait_idle()
    inject(63.5, 575)
    time.sleep(2)
    c2 = one_card("替换后仍只有一张")
    if c2:
        check("体重更新为 63.5", "63.5" in str(c2["context"].get("title_placeholders")), str(c2["context"].get("title_placeholders")))

    print("\n[D2] 手动归属给小红 → 卡片被撤掉")
    req("POST", "/api/services/afu_scale/assign_last_measurement", {"user": "小红"})
    time.sleep(2)
    check("没有卡片了", len(cards()) == 0, str(len(cards())))

    print("\n[D3] 菜单「归给现有用户」：小红体重变了，用 64.0kg 重置她的基准")
    wait_idle()
    inject(64.0, 580)
    time.sleep(2)
    card = one_card("出现卡片")
    if card:
        card_step(card["flow_id"], step="assign_existing")
        # 取小红的 id
        form = card_step(card["flow_id"])
        opts = form["data_schema"][0]["options"]
        rid = next(k for k, v in opts if v == "小红")
        done = card_step(card["flow_id"], data={"id": rid})
        check("流程以 user_updated 结束", done.get("reason") == "user_updated", str(done.get("reason")))
    s = settle(2)
    check("小红 体重=64.0（读数被采纳）", wf(s, R + "体重") == 64.0, s.get(R + "体重"))
    check("归属=小红, manual", s.get(L) == "小红" and s.get(C) == "manual", f"{s.get(L)}/{s.get(C)}")
    check("小明 体重 没被影响", wf(s, X + "体重") == x0, f"{s.get(X + '体重')} vs {x0}")
    wait_idle()
    inject(64.4, 585)
    s = settle(2)
    check("基准已重置：64.4 → 直接认成小红 high", s.get(L) == "小红" and s.get(C) == "high", f"{s.get(L)}/{s.get(C)}")
    wait_idle()
    inject(55.3, 645)
    s = settle(2)
    check("旧基准已丢掉：55.3 不再是小红 → 访客", s.get(L) == "访客", s.get(L))
    check("同名 55.3 弹出新卡片", len(cards()) == 1, str(len(cards())))

    print("\n[D4] 菜单「新建用户」：重名被拒绝，改名后创建 小刚")
    card = cards()[0]
    card_step(card["flow_id"], step="new_user")
    dup = card_step(card["flow_id"], data={"name": "小明", "height_cm": 170, "sex": "male", "age": 35})
    check("重名被拒绝", dup.get("errors", {}).get("name") == "name_taken", str(dup.get("errors")))
    done = card_step(card["flow_id"], data={"name": "小刚", "height_cm": 170, "sex": "male", "age": 35})
    check("流程以 user_added 结束", done.get("reason") == "user_added", str(done.get("reason")))
    time.sleep(8)
    s = states()
    check("小刚 体重=55.3（那次读数被采纳）", wf(s, G + "体重") == 55.3, s.get(G + "体重"))
    check("小刚 体脂率有值", wf(s, G + "体脂率") is not None, s.get(G + "体脂率"))
    check("归属=小刚, manual", s.get(L) == "小刚" and s.get(C) == "manual", f"{s.get(L)}/{s.get(C)}")

    print(f"\n失败: {failures}")
    return failures


# ---- zero ----------------------------------------------------------------

def zero():
    eid = entry_id()
    print("\n[Z1] 把所有用户删光")
    for _ in range(10):
        f = req("POST", "/api/config/config_entries/options/flow", {"handler": eid})
        if "remove_user" not in f.get("menu_options", []):
            break
        f = req("POST", f"/api/config/config_entries/options/flow/{f['flow_id']}", {"next_step_id": "remove_user"})
        uid = f["data_schema"][0]["options"][0][0]
        req("POST", f"/api/config/config_entries/options/flow/{f['flow_id']}", {"id": uid})
        time.sleep(5)
    f = req("POST", "/api/config/config_entries/options/flow", {"handler": eid})
    check("菜单里不再有 编辑/删除 用户", set(f.get("menu_options", [])) == {"add_user", "debug"}, str(f.get("menu_options")))
    req("DELETE", f"/api/config/config_entries/options/flow/{f['flow_id']}")

    print("\n[Z2] 0 个用户：称重 → 访客 + 卡片 → 直接新建")
    wait_idle()
    inject(72.0, 530)
    s = settle(2)
    check("归属=访客", s.get(L) == "访客", s.get(L))
    card = one_card("出现一张卡片")
    if card:
        step = card_step(card["flow_id"])
        check("直接是新建表单", step.get("step_id") == "new_user", str(step.get("step_id")))
        done = card_step(card["flow_id"], data={"name": "小明", "height_cm": 175, "sex": "male", "age": 30})
        check("user_added", done.get("reason") == "user_added", str(done.get("reason")))
    time.sleep(8)
    s = states()
    check("小明 体重=72.0", wf(s, X + "体重") == 72.0, s.get(X + "体重"))
    print(f"\n失败: {failures}")
    return failures


def main():
    login()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "reset":
        reset()
    elif cmd == "state":
        for k, v in sorted(states().items()):
            print(f"  {k:36} {v}")
    else:
        fn = {"fresh": fresh, "run": run, "discovery": run_discovery, "zero": zero}.get(cmd)
        if fn is None:
            raise SystemExit(__doc__)
        sys.exit(1 if fn() else 0)


if __name__ == "__main__":
    main()
