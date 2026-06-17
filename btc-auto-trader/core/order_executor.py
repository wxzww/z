"""
订单执行模块 - 将LLM的决策指令转化为Binance API调用

修复：
 - 分批止盈：有TP2时，TP1平半仓，TP2平另半仓
 - 防重复挂单：下止损/止盈前检查已有数量，超过持仓量的不挂
 - 清理加验证：cancel后重新查询确认，未清干净则重试
"""
import time
import hmac
import hashlib
from urllib.parse import urlencode
import requests as http_requests
from binance.client import Client
from binance.exceptions import BinanceAPIException
from utils.logger import get_logger
from utils.config import AppConfig

logger = get_logger("btc_trader.executor")

ACTION_PRIORITY = {
    "cancel_all": 0, "cancel_order": 1,
    "close_long": 2, "close_short": 2,
    "reduce_position": 3,
    "move_stop_loss": 4, "move_take_profit": 4,
    "open_long": 5, "open_short": 5, "add_position": 5, "place_limit_entry": 5,
    "place_stop_loss": 6, "place_take_profit": 6,
    "replace_order": 7, "hold": 99, "no_action": 99,
}


class OrderExecutor:

    def __init__(self, config: AppConfig, binance_client: Client):
        self.config = config
        self.client = binance_client
        self.symbol = config.binance.symbol
        self.leverage = config.binance.leverage
        self._futures_base = "https://fapi.binance.com"
        self._api_key = binance_client.API_KEY
        self._api_secret = binance_client.API_SECRET
        self._pending_entries: dict[str, dict] = {}

    # ============================================================
    # Algo Order API
    # ============================================================

    def _sign_params(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        qs = urlencode(params)
        sig = hmac.new(self._api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _algo_request(self, method: str, path: str, params: dict) -> dict:
        url = f"{self._futures_base}{path}"
        signed = self._sign_params(params)
        headers = {"X-MBX-APIKEY": self._api_key}
        if method == "POST":
            resp = http_requests.post(url, params=signed, headers=headers, timeout=10)
        elif method == "GET":
            resp = http_requests.get(url, params=signed, headers=headers, timeout=10)
        elif method == "DELETE":
            resp = http_requests.delete(url, params=signed, headers=headers, timeout=10)
        else:
            raise ValueError(f"Unsupported: {method}")
        data = resp.json()
        if resp.status_code != 200:
            raise Exception(f"Algo API error(code={data.get('code')}): {data.get('msg')}")
        return data

    def _place_algo_order(self, side: str, order_type: str,
                          trigger_price: float, quantity: float,
                          purpose: str = "") -> dict:
        if trigger_price is None or trigger_price <= 0:
            logger.error(f"Algo跳过: {purpose}, price无效: {trigger_price}")
            return {}
        position_side = "LONG" if side.upper() == "SELL" else "SHORT"
        params = {
            "algoType": "CONDITIONAL", "symbol": self.symbol,
            "side": side, "positionSide": position_side,
            "type": order_type, "quantity": str(quantity),
            "triggerPrice": str(round(trigger_price, 2)),
            "workingType": "MARK_PRICE",
        }
        try:
            result = self._algo_request("POST", "/fapi/v1/algoOrder", params)
            logger.info(f"Algo挂单: {purpose}, type={order_type}, "
                        f"trigger={trigger_price}, qty={quantity}, algoId={result.get('algoId')}")
            return result
        except Exception as e:
            logger.error(f"Algo挂单失败: {purpose}, {e}")
            return {}

    def _get_open_algo_orders(self) -> list:
        try:
            result = self._algo_request("GET", "/fapi/v1/openAlgoOrders",
                                        {"symbol": self.symbol})
            return result.get("orders", []) if isinstance(result, dict) else result
        except Exception as e:
            logger.warning(f"获取Algo单失败: {e}")
            return []

    def _cancel_algo_order(self, algo_id) -> bool:
        try:
            self._algo_request("DELETE", "/fapi/v1/algoOrder", {"algoId": str(algo_id)})
            logger.info(f"取消Algo: {algo_id}")
            return True
        except Exception as e:
            logger.warning(f"取消Algo失败: {algo_id}, {e}")
            return False

    # ============================================================
    # 防重复挂单（问题3核心修复）
    # ============================================================

    def _get_existing_algo_qty(self, order_type: str) -> float:
        """查询交易所上指定类型的 Algo 条件单总数量"""
        try:
            algo_orders = self._get_open_algo_orders()
            total = 0.0
            for o in algo_orders:
                if o.get("type") == order_type:
                    total += float(o.get("quantity", 0) or o.get("origQty", 0))
            return total
        except Exception:
            return 0.0

    def _get_position_qty(self) -> float:
        """获取当前持仓绝对数量"""
        try:
            positions = self.client.futures_position_information(symbol=self.symbol)
            for p in positions:
                amt = abs(float(p["positionAmt"]))
                if amt > 0:
                    return amt
        except Exception:
            pass
        return 0.0

    def _safe_place_stop(self, side: str, stop_price: float,
                         quantity: float, purpose: str) -> dict:
        """下止损前检查：已有止损数量 + 本次 ≤ 持仓量才挂"""
        existing = self._get_existing_algo_qty("STOP_MARKET")
        pos_qty = self._get_position_qty()
        if existing >= pos_qty and pos_qty > 0:
            logger.info(f"跳过止损: 已有止损{existing} >= 持仓{pos_qty}, {purpose}")
            return {}
        # 不超过持仓量
        allowed = max(pos_qty - existing, 0)
        qty = min(quantity, allowed) if allowed > 0 else quantity
        if qty <= 0:
            return {}
        return self._place_algo_order(side, "STOP_MARKET", stop_price, qty, purpose)

    def _safe_place_tp(self, side: str, price: float,
                       quantity: float, purpose: str) -> dict:
        """下止盈前检查：已有止盈数量 + 本次 ≤ 持仓量才挂"""
        existing = self._get_existing_algo_qty("TAKE_PROFIT_MARKET")
        pos_qty = self._get_position_qty()
        if existing >= pos_qty and pos_qty > 0:
            logger.info(f"跳过止盈: 已有止盈{existing} >= 持仓{pos_qty}, {purpose}")
            return {}
        allowed = max(pos_qty - existing, 0)
        qty = min(quantity, allowed) if allowed > 0 else quantity
        if qty <= 0:
            return {}
        return self._place_algo_order(side, "TAKE_PROFIT_MARKET", price, qty, purpose)

    # ============================================================
    # 限价入场单成交检测
    # ============================================================

    def check_pending_fills(self):
        if not self._pending_entries:
            return
        filled_ids = []
        for order_id, info in self._pending_entries.items():
            try:
                order = self.client.futures_get_order(symbol=self.symbol, orderId=int(order_id))
                status = order.get("status", "")
                if status == "FILLED":
                    logger.info(f"限价入场已成交: {order_id}")
                    qty = float(order.get("executedQty", info["quantity"]))
                    close_side = "SELL" if info["side"] == "BUY" else "BUY"
                    self._place_stop_order(close_side, info["stop_loss_price"], qty,
                                           f"限价成交补止损({order_id})")
                    tp2 = info.get("take_profit_2_price")
                    if tp2:
                        half = self._round_quantity(qty / 2)
                        self._place_tp_order(close_side, info["take_profit_price"], half,
                                             f"限价成交补止盈1({order_id})")
                        rest = self._round_quantity(qty - half)
                        if rest > 0:
                            self._place_tp_order(close_side, tp2, rest,
                                                 f"限价成交补止盈2({order_id})")
                    else:
                        self._place_tp_order(close_side, info["take_profit_price"], qty,
                                             f"限价成交补止盈({order_id})")
                    filled_ids.append(order_id)
                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    filled_ids.append(order_id)
            except Exception as e:
                logger.warning(f"查询限价单失败: {order_id}, {e}")
        for oid in filled_ids:
            self._pending_entries.pop(oid, None)

    # ============================================================
    # 主入口
    # ============================================================

    def execute_actions(self, actions: list, account: dict) -> list[dict]:
        if not actions:
            return []
        sorted_actions = sorted(actions,
                                key=lambda a: ACTION_PRIORITY.get(a.get("action", ""), 50))

        # ---- 预处理：多个 place_take_profit 自动均分持仓 ----
        tp_actions = [a for a in sorted_actions if a.get("action") == "place_take_profit"]
        if tp_actions:
            pos_qty = self._get_position_qty()
            if pos_qty > 0:
                n = len(tp_actions)
                base = self._round_quantity(pos_qty / n)
                for i, tp in enumerate(tp_actions):
                    if i < n - 1:
                        tp["_auto_qty"] = base
                    else:
                        # 最后一个拿剩余，避免精度丢失
                        tp["_auto_qty"] = self._round_quantity(pos_qty - base * (n - 1))
                logger.info(f"止盈数量自动分配: {n}个TP, 持仓{pos_qty}, "
                            f"各分配{[tp.get('_auto_qty') for tp in tp_actions]}")

        results = []
        for action in sorted_actions:
            act_type = action.get("action", "")
            if act_type in ("hold", "no_action"):
                results.append({"action": act_type, "status": "skipped",
                                "reason": action.get("reason", "")})
                continue
            try:
                result = self._dispatch(action, act_type, account)
                results.append(result)
                logger.info(f"执行成功: {act_type} → {result.get('status')}")
            except BinanceAPIException as e:
                results.append({"action": act_type, "status": "failed", "error": str(e)})
                logger.error(f"执行失败: {act_type} → {e}")
                if act_type in ("close_long", "close_short"):
                    logger.error("平仓失败，取消后续开仓")
                    break
            except Exception as e:
                results.append({"action": act_type, "status": "error", "error": str(e)})
                logger.error(f"执行异常: {act_type} → {e}")
        return results

    def _dispatch(self, action, act_type, account):
        handlers = {
            "open_long": self._open_long, "open_short": self._open_short,
            "close_long": self._close_position, "close_short": self._close_position,
            "add_position": self._add_position, "reduce_position": self._reduce_position,
            "place_stop_loss": self._place_stop_loss,
            "place_take_profit": self._place_take_profit,
            "move_stop_loss": self._move_stop_loss,
            "move_take_profit": self._move_take_profit,
            "place_limit_entry": self._place_limit_entry,
            "cancel_order": self._cancel_order, "cancel_all": self._cancel_all,
            "replace_order": self._replace_order,
        }
        handler = handlers.get(act_type)
        if not handler:
            return {"action": act_type, "status": "unknown_action"}
        return handler(action, account)

    # ============================================================
    # 开仓 — 分批止盈修复
    # ============================================================

    def _open_long(self, action: dict, account: dict) -> dict:
        quantity = self._calc_quantity(action, account)
        if action.get("order_type", "market") == "market":
            order = self.client.futures_create_order(
                symbol=self.symbol, side="BUY", type="MARKET",
                quantity=quantity, positionSide="LONG")
        else:
            order = self.client.futures_create_order(
                symbol=self.symbol, side="BUY", type="LIMIT",
                price=str(action["price"]), quantity=quantity,
                timeInForce="GTC", positionSide="LONG")
        logger.info(f"开多仓: qty={quantity}, id={order['orderId']}")

        # 止损（全仓）
        sl = self._place_stop_order("SELL", action["stop_loss_price"], quantity, "开多止损")

        # 止盈（分批：有TP2时各一半，无TP2时全仓）
        has_tp2 = bool(action.get("take_profit_2_price"))
        tp1_qty = self._round_quantity(quantity / 2) if has_tp2 else quantity
        tp = self._place_tp_order("SELL", action["take_profit_price"], tp1_qty,
                                  f"开多止盈1({'半仓' if has_tp2 else '全仓'})")
        if has_tp2:
            tp2_qty = self._round_quantity(quantity - tp1_qty)
            if tp2_qty > 0:
                self._place_tp_order("SELL", action["take_profit_2_price"], tp2_qty,
                                     "开多止盈2(剩余)")

        return {"action": "open_long", "status": "success",
                "order_id": str(order["orderId"]), "quantity": quantity,
                "price": float(order.get("avgPrice", 0)) or action.get("price")}

    def _open_short(self, action: dict, account: dict) -> dict:
        quantity = self._calc_quantity(action, account)
        if action.get("order_type", "market") == "market":
            order = self.client.futures_create_order(
                symbol=self.symbol, side="SELL", type="MARKET",
                quantity=quantity, positionSide="SHORT")
        else:
            order = self.client.futures_create_order(
                symbol=self.symbol, side="SELL", type="LIMIT",
                price=str(action["price"]), quantity=quantity,
                timeInForce="GTC", positionSide="SHORT")
        logger.info(f"开空仓: qty={quantity}, id={order['orderId']}")

        sl = self._place_stop_order("BUY", action["stop_loss_price"], quantity, "开空止损")

        has_tp2 = bool(action.get("take_profit_2_price"))
        tp1_qty = self._round_quantity(quantity / 2) if has_tp2 else quantity
        tp = self._place_tp_order("BUY", action["take_profit_price"], tp1_qty,
                                  f"开空止盈1({'半仓' if has_tp2 else '全仓'})")
        if has_tp2:
            tp2_qty = self._round_quantity(quantity - tp1_qty)
            if tp2_qty > 0:
                self._place_tp_order("BUY", action["take_profit_2_price"], tp2_qty,
                                     "开空止盈2(剩余)")

        return {"action": "open_short", "status": "success",
                "order_id": str(order["orderId"]), "quantity": quantity}

    # ============================================================
    # 平仓/减仓
    # ============================================================

    def _close_position(self, action: dict, account: dict) -> dict:
        act_type = action.get("action")
        side = "SELL" if act_type == "close_long" else "BUY"
        positions = self.client.futures_position_information(symbol=self.symbol)
        pos_qty = 0
        for p in positions:
            amt = float(p["positionAmt"])
            if amt != 0:
                pos_qty = abs(amt)
                break
        if pos_qty == 0:
            return {"action": act_type, "status": "no_position"}

        qty_pct = action.get("quantity_pct", 1.0)
        close_qty = self._round_quantity(pos_qty * qty_pct)
        if close_qty <= 0:
            close_qty = pos_qty

        # Hedge Mode: 用 positionSide 而不是 reduceOnly
        pos_side = "LONG" if side == "SELL" else "SHORT"
        order = self.client.futures_create_order(
            symbol=self.symbol, side=side, type="MARKET",
            quantity=close_qty, positionSide=pos_side)
        logger.info(f"平仓: side={side}, qty={close_qty}")

        if qty_pct >= 0.99:
            self._cleanup_orphan_orders()
        return {"action": act_type, "status": "success",
                "order_id": str(order["orderId"]), "quantity": close_qty}

    def _reduce_position(self, action, account):
        positions = self.client.futures_position_information(symbol=self.symbol)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                action["action"] = "close_long"
            elif amt < 0:
                action["action"] = "close_short"
            else:
                return {"action": "reduce_position", "status": "no_position"}
        return self._close_position(action, account)

    def _add_position(self, action, account):
        positions = self.client.futures_position_information(symbol=self.symbol)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                return self._open_long(action, account)
            elif amt < 0:
                return self._open_short(action, account)
        return self._open_long(action, account) if action.get("side", "buy") == "buy" \
            else self._open_short(action, account)

    # ============================================================
    # 止损止盈管理 — 使用 _safe_place 防重复
    # ============================================================

    def _place_stop_order(self, side, stop_price, quantity, purpose=""):
        """开仓伴随的止损（直接挂，因为是新开仓）"""
        return self._place_algo_order(side, "STOP_MARKET", stop_price, quantity, purpose)

    def _place_tp_order(self, side, price, quantity, purpose=""):
        """开仓伴随的止盈（直接挂，因为是新开仓）"""
        return self._place_algo_order(side, "TAKE_PROFIT_MARKET", price, quantity, purpose)

    def _place_stop_loss(self, action, account):
        """LLM指令：补挂止损 — 用 _safe_place 防重复"""
        price = action.get("price") or action.get("stop_loss_price")
        if not price:
            return {"action": "place_stop_loss", "status": "missing_price"}
        positions = self.client.futures_position_information(symbol=self.symbol)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                order = self._safe_place_stop("SELL", price, abs(amt), "LLM补止损(多)")
                return {"action": "place_stop_loss",
                        "status": "success" if order else "skipped_duplicate"}
            elif amt < 0:
                order = self._safe_place_stop("BUY", price, abs(amt), "LLM补止损(空)")
                return {"action": "place_stop_loss",
                        "status": "success" if order else "skipped_duplicate"}
        return {"action": "place_stop_loss", "status": "no_position"}

    def _place_take_profit(self, action, account):
        """LLM指令：补挂止盈 — 用 _safe_place 防重复"""
        price = action.get("price") or action.get("take_profit_price")
        if not price:
            return {"action": "place_take_profit", "status": "missing_price"}
        positions = self.client.futures_position_information(symbol=self.symbol)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                # 优先用 execute_actions 预处理分配的数量
                qty = action.get("_auto_qty") or abs(amt)
                order = self._safe_place_tp("SELL", price, qty, "LLM补止盈(多)")
                return {"action": "place_take_profit",
                        "status": "success" if order else "skipped_duplicate"}
            elif amt < 0:
                qty = action.get("_auto_qty") or abs(amt)
                order = self._safe_place_tp("BUY", price, qty, "LLM补止盈(空)")
                return {"action": "place_take_profit",
                        "status": "success" if order else "skipped_duplicate"}
        return {"action": "place_take_profit", "status": "no_position"}

    def _move_stop_loss(self, action, account):
        new_price = action.get("new_stop_price")
        if not new_price:
            return {"action": "move_stop_loss", "status": "missing_price"}
        # 撤旧
        for o in self._get_open_algo_orders():
            if o.get("type") == "STOP_MARKET":
                self._cancel_algo_order(o.get("algoId"))
        # 挂新
        positions = self.client.futures_position_information(symbol=self.symbol)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                self._place_stop_order("SELL", new_price, abs(amt), f"移止损→{new_price}")
                return {"action": "move_stop_loss", "status": "success", "new_price": new_price}
            elif amt < 0:
                self._place_stop_order("BUY", new_price, abs(amt), f"移止损→{new_price}")
                return {"action": "move_stop_loss", "status": "success", "new_price": new_price}
        return {"action": "move_stop_loss", "status": "no_position"}

    def _move_take_profit(self, action, account):
        new_price = action.get("new_tp_price")
        if not new_price:
            return {"action": "move_take_profit", "status": "missing_price"}
        for o in self._get_open_algo_orders():
            if o.get("type") == "TAKE_PROFIT_MARKET":
                self._cancel_algo_order(o.get("algoId"))
        positions = self.client.futures_position_information(symbol=self.symbol)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                self._place_tp_order("SELL", new_price, abs(amt), f"移止盈→{new_price}")
            elif amt < 0:
                self._place_tp_order("BUY", new_price, abs(amt), f"移止盈→{new_price}")
        return {"action": "move_take_profit", "status": "success", "new_price": new_price}

    # ============================================================
    # 挂单管理
    # ============================================================

    def _place_limit_entry(self, action, account):
        quantity = self._calc_quantity(action, account)
        side = "BUY" if action.get("side", "buy") == "buy" else "SELL"
        pos_side = "LONG" if side == "BUY" else "SHORT"
        order = self.client.futures_create_order(
            symbol=self.symbol, side=side, type="LIMIT",
            price=str(action["price"]), quantity=quantity,
            timeInForce="GTC", positionSide=pos_side)
        oid = str(order["orderId"])
        logger.info(f"限价入场: side={side}, price={action['price']}, qty={quantity}, id={oid}")
        self._pending_entries[oid] = {
            "side": side, "quantity": quantity, "price": action["price"],
            "stop_loss_price": action["stop_loss_price"],
            "take_profit_price": action["take_profit_price"],
            "take_profit_2_price": action.get("take_profit_2_price"),
            "created_at": time.time(),
        }
        return {"action": "place_limit_entry", "status": "success",
                "order_id": oid, "quantity": quantity}

    def _cancel_order(self, action, account):
        order_id = action.get("cancel_order_id")
        if not order_id:
            return {"action": "cancel_order", "status": "missing_order_id"}

        # 第一步：尝试普通订单撤销
        try:
            self.client.futures_cancel_order(symbol=self.symbol, orderId=int(order_id))
            self._pending_entries.pop(str(order_id), None)
            logger.info(f"撤单(普通): {order_id}")
            return {"action": "cancel_order", "status": "success", "order_id": order_id}
        except BinanceAPIException as e:
            if "Unknown order" not in str(e):
                raise
            # 普通接口找不到，继续尝试 Algo 接口

        # 第二步：尝试 Algo 条件单撤销
        if self._cancel_algo_order(order_id):
            logger.info(f"撤单(Algo): {order_id}")
            return {"action": "cancel_order", "status": "success", "order_id": order_id}

        # 两边都找不到
        self._pending_entries.pop(str(order_id), None)
        logger.warning(f"撤单失败: {order_id} 在普通和Algo接口都找不到")
        return {"action": "cancel_order", "status": "already_gone"}

    def _cancel_all(self, action, account):
        self.client.futures_cancel_all_open_orders(symbol=self.symbol)
        self._pending_entries.clear()
        logger.info("撤销所有挂单")
        return {"action": "cancel_all", "status": "success"}

    def _replace_order(self, action, account):
        self._cancel_order(action, account)
        side = action.get("side", "buy").upper()
        pos_side = "LONG" if side == "BUY" else "SHORT"
        qty = (self._calc_quantity(action, account)
               if action.get("quantity_pct") else action.get("quantity", 0))
        order = self.client.futures_create_order(
            symbol=self.symbol, side=side, type="LIMIT",
            price=str(action["price"]), quantity=qty,
            timeInForce="GTC", positionSide=pos_side)
        return {"action": "replace_order", "status": "success",
                "new_order_id": str(order["orderId"])}

    # ============================================================
    # 清理 — 带重试验证（问题6修复）
    # ============================================================

    def _cleanup_orphan_orders(self, max_retries: int = 2):
        """全平后清理所有关联挂单，验证清理干净"""
        for attempt in range(max_retries + 1):
            # 清普通单
            try:
                self.client.futures_cancel_all_open_orders(symbol=self.symbol)
            except Exception as e:
                logger.warning(f"清理普通单失败: {e}")

            # 清Algo单
            algo_orders = self._get_open_algo_orders()
            for o in algo_orders:
                aid = o.get("algoId")
                if aid:
                    self._cancel_algo_order(aid)

            # 验证
            time.sleep(0.5)
            remaining_normal = self.client.futures_get_open_orders(symbol=self.symbol)
            remaining_algo = self._get_open_algo_orders()
            total_remaining = len(remaining_normal) + len(remaining_algo)

            if total_remaining == 0:
                logger.info(f"清理完成（第{attempt+1}轮）")
                break
            else:
                logger.warning(f"清理不完整: 剩余{total_remaining}单，重试...")

        self._pending_entries.clear()

    # ============================================================
    # 辅助
    # ============================================================

    def _calc_quantity(self, action, account):
        pct = action.get("quantity_pct", 0.10)
        total = account.get("total_balance", 0)
        price = action.get("price") or self._get_price()
        if price <= 0:
            raise ValueError("无法获取价格")
        return self._round_quantity(total * pct * self.leverage / price)

    def _round_quantity(self, qty):
        return round(max(qty, 0), 3)

    def _get_price(self):
        return float(self.client.futures_symbol_ticker(symbol=self.symbol)["price"])

    def emergency_cancel_all(self):
        try:
            self.client.futures_cancel_all_open_orders(symbol=self.symbol)
        except Exception as e:
            logger.critical(f"紧急撤普通单失败: {e}")
        try:
            for o in self._get_open_algo_orders():
                self._cancel_algo_order(o.get("algoId"))
        except Exception as e:
            logger.critical(f"紧急撤Algo单失败: {e}")
        self._pending_entries.clear()