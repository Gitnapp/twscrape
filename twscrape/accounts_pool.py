import asyncio
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TypedDict
import time

from fake_useragent import UserAgent
from httpx import HTTPStatusError

from .account import Account
from .db import execute, fetchall, fetchone
from .logger import logger
from .login import LoginConfig, login
from .utils import get_env_bool, parse_cookies, utc


class NoAccountError(Exception):
    pass


class AccountInfo(TypedDict):
    username: str
    logged_in: bool
    active: bool
    last_used: datetime | None
    total_req: int
    error_msg: str | None


def guess_delim(line: str):
    lp, rp = tuple([x.strip() for x in line.split("username")])
    return rp[0] if not lp else lp[-1]


class AccountsPool:
    _order_by: str = "RANDOM()"
    # _order_by: str = "username"

    def __init__(
        self,
        db_file="accounts.db",
        login_config: LoginConfig | None = None,
        raise_when_no_account=False,
    ):
        self._db_file = db_file
        self._login_config = login_config or LoginConfig()
        self._raise_when_no_account = raise_when_no_account
        self._initialized = False  # 添加初始化标志
        
        # 移除asyncio.run调用

    async def ensure_initialized(self):
        """确保数据库表已初始化"""
        if not self._initialized:
            await self._init_custom_limits_tables()
            self._initialized = True
            return True
        return False

    async def _init_custom_limits_tables(self):
        """初始化自定义限制相关表"""
        # 自定义限制表
        await execute(self._db_file, """
            CREATE TABLE IF NOT EXISTS custom_limits (
                id INTEGER PRIMARY KEY,
                queue TEXT,
                username TEXT DEFAULT '*',
                hourly_limit INTEGER DEFAULT -1,
                daily_limit INTEGER DEFAULT -1,
                UNIQUE(queue, username)
            )
        """)
        
        # 使用记录表
        await execute(self._db_file, """
            CREATE TABLE IF NOT EXISTS usage_stats (
                id INTEGER PRIMARY KEY,
                queue TEXT,
                username TEXT,
                timestamp INTEGER,
                req_count INTEGER DEFAULT 1
            )
        """)
        
        # 创建索引以提高查询性能
        await execute(self._db_file, """
            CREATE INDEX IF NOT EXISTS idx_usage_stats_queue_username_timestamp 
            ON usage_stats(queue, username, timestamp)
        """)
        
        await execute(self._db_file, """
            CREATE INDEX IF NOT EXISTS idx_custom_limits_queue_username 
            ON custom_limits(queue, username)
        """)

    async def load_from_file(self, filepath: str, line_format: str):
        line_delim = guess_delim(line_format)
        tokens = line_format.split(line_delim)

        required = set(["username", "password", "email", "email_password"])
        if not required.issubset(tokens):
            raise ValueError(f"Invalid line format: {line_format}")

        accounts = []
        with open(filepath, "r") as f:
            lines = f.read().split("\n")
            lines = [x.strip() for x in lines if x.strip()]

            for line in lines:
                data = [x.strip() for x in line.split(line_delim)]
                if len(data) < len(tokens):
                    raise ValueError(f"Invalid line: {line}")

                data = data[: len(tokens)]
                vals = {k: v for k, v in zip(tokens, data) if k != "_"}
                accounts.append(vals)

        for x in accounts:
            await self.add_account(**x)

    async def add_account(
        self,
        username: str,
        password: str,
        email: str,
        email_password: str,
        user_agent: str | None = None,
        proxy: str | None = None,
        cookies: str | None = None,
        mfa_code: str | None = None,
    ):
        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if rs:
            logger.warning(f"Account {username} already exists")
            return

        account = Account(
            username=username,
            password=password,
            email=email,
            email_password=email_password,
            user_agent=user_agent or UserAgent().safari,
            active=False,
            locks={},
            stats={},
            headers={},
            cookies=parse_cookies(cookies) if cookies else {},
            proxy=proxy,
            mfa_code=mfa_code,
        )

        if "ct0" in account.cookies:
            account.active = True

        await self.save(account)
        logger.info(f"Account {username} added successfully (active={account.active})")

    async def delete_accounts(self, usernames: str | list[str]):
        usernames = usernames if isinstance(usernames, list) else [usernames]
        usernames = list(set(usernames))
        if not usernames:
            logger.warning("No usernames provided")
            return

        qs = f"""DELETE FROM accounts WHERE username IN ({",".join([f'"{x}"' for x in usernames])})"""
        await execute(self._db_file, qs)

    async def delete_inactive(self):
        qs = "DELETE FROM accounts WHERE active = false"
        await execute(self._db_file, qs)

    async def get(self, username: str):
        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if not rs:
            raise ValueError(f"Account {username} not found")
        return Account.from_rs(rs)

    async def get_all(self):
        qs = "SELECT * FROM accounts"
        rs = await fetchall(self._db_file, qs)
        return [Account.from_rs(x) for x in rs]

    async def get_account(self, username: str):
        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if not rs:
            return None
        return Account.from_rs(rs)

    async def save(self, account: Account):
        data = account.to_rs()
        cols = list(data.keys())

        qs = f"""
        INSERT INTO accounts ({",".join(cols)}) VALUES ({",".join([f":{x}" for x in cols])})
        ON CONFLICT(username) DO UPDATE SET {",".join([f"{x}=excluded.{x}" for x in cols])}
        """
        await execute(self._db_file, qs, data)

    async def login(self, account: Account):
        try:
            await login(account, cfg=self._login_config)
            logger.info(f"Logged in to {account.username} successfully")
            return True
        except HTTPStatusError as e:
            rep = e.response
            logger.error(f"Failed to login '{account.username}': {rep.status_code} - {rep.text}")
            return False
        except Exception as e:
            logger.error(f"Failed to login '{account.username}': {e}")
            return False
        finally:
            await self.save(account)

    async def login_all(self, usernames: list[str] | None = None):
        if usernames is None:
            qs = "SELECT * FROM accounts WHERE active = false AND error_msg IS NULL"
        else:
            us = ",".join([f"'{x}'" for x in usernames])
            qs = f"SELECT * FROM accounts WHERE username IN ({us})"

        rs = await fetchall(self._db_file, qs)
        accounts = [Account.from_rs(rs) for rs in rs]
        # await asyncio.gather(*[login(x) for x in self.accounts])

        counter = {"total": len(accounts), "success": 0, "failed": 0}
        for i, x in enumerate(accounts, start=1):
            logger.info(f"[{i}/{len(accounts)}] Logging in {x.username} - {x.email}")
            status = await self.login(x)
            counter["success" if status else "failed"] += 1
        return counter

    async def relogin(self, usernames: str | list[str]):
        usernames = usernames if isinstance(usernames, list) else [usernames]
        usernames = list(set(usernames))
        if not usernames:
            logger.warning("No usernames provided")
            return

        qs = f"""
        UPDATE accounts SET
            active = false,
            locks = json_object(),
            last_used = NULL,
            error_msg = NULL,
            headers = json_object(),
            cookies = json_object(),
            user_agent = '{UserAgent().safari}'
        WHERE username IN ({",".join([f"'{x}'" for x in usernames])})
        """

        await execute(self._db_file, qs)
        await self.login_all(usernames)

    async def relogin_failed(self):
        qs = "SELECT username FROM accounts WHERE active = false AND error_msg IS NOT NULL"
        rs = await fetchall(self._db_file, qs)
        await self.relogin([x["username"] for x in rs])

    async def reset_locks(self):
        qs = "UPDATE accounts SET locks = json_object()"
        await execute(self._db_file, qs)

    async def set_active(self, username: str, active: bool):
        qs = "UPDATE accounts SET active = :active WHERE username = :username"
        await execute(self._db_file, qs, {"username": username, "active": active})

    async def lock_until(self, username: str, queue: str, unlock_at: int, req_count=0):
        qs = f"""
        UPDATE accounts SET
            locks = json_set(locks, '$.{queue}', datetime({unlock_at}, 'unixepoch')),
            stats = json_set(stats, '$.{queue}', COALESCE(json_extract(stats, '$.{queue}'), 0) + {req_count}),
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE username = :username
        """
        await execute(self._db_file, qs, {"username": username})

    async def unlock(self, username: str, queue: str, req_count: int = 1):
        qs = f"""
        UPDATE accounts SET
            locks = json_remove(locks, '$.{queue}'),
            stats = json_set(stats, '$.{queue}', COALESCE(json_extract(stats, '$.{queue}'), 0) + {req_count}),
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE username = :username
        """
        await execute(self._db_file, qs, {"username": username})
        
        # 记录使用量
        await self.record_usage(queue, username, req_count)

    async def _get_and_lock(self, queue: str, condition: str):
        # if space in condition, it's a subquery, otherwise it's username
        condition = f"({condition})" if " " in condition else f"'{condition}'"

        if int(sqlite3.sqlite_version_info[1]) >= 35:
            qs = f"""
            UPDATE accounts SET
                locks = json_set(locks, '$.{queue}', datetime('now', '+15 minutes')),
                last_used = datetime({utc.ts()}, 'unixepoch')
            WHERE username = {condition}
            RETURNING *
            """
            rs = await fetchone(self._db_file, qs)
        else:
            tx = uuid.uuid4().hex
            qs = f"""
            UPDATE accounts SET
                locks = json_set(locks, '$.{queue}', datetime('now', '+15 minutes')),
                last_used = datetime({utc.ts()}, 'unixepoch'),
                _tx = '{tx}'
            WHERE username = {condition}
            """
            await execute(self._db_file, qs)

            qs = f"SELECT * FROM accounts WHERE _tx = '{tx}'"
            rs = await fetchone(self._db_file, qs)

        return Account.from_rs(rs) if rs else None

    async def get_for_queue(self, queue: str):
        # 先尝试获取可用账号列表
        q = f"""
        SELECT username FROM accounts
        WHERE active = true AND (
            locks IS NULL
            OR json_extract(locks, '$.{queue}') IS NULL
            OR json_extract(locks, '$.{queue}') < datetime('now')
        )
        ORDER BY {self._order_by}
        """
        
        # 获取符合条件的所有账号列表
        accounts_rs = await fetchall(self._db_file, q)
        if not accounts_rs:
            return None
            
        # 检查每个账号的自定义限制
        for acc_row in accounts_rs:
            username = acc_row["username"]
            
            # 检查该账号是否达到限制
            if await self._check_custom_limits(queue, username):
                # 通过限制检查，可以使用这个账号
                return await self._get_and_lock(queue, username)
                
        # 所有账号都达到限制
        return None
        
    async def _check_custom_limits(self, queue: str, username: str) -> bool:
        """检查账号在指定队列上是否满足自定义限制
        
        Args:
            queue: 队列名
            username: 账号用户名
            
        Returns:
            bool: 如果满足限制返回True，否则返回False
        """
        # 获取账号的特定限制
        hourly_limit, daily_limit = await self.get_limit(queue, username)
        
        # 获取全局限制(作为备用)
        global_hourly, global_daily = await self.get_limit(queue)
        
        # 使用较小的限制值(如果账号限制是-1表示无限制，则使用全局限制)
        if hourly_limit == -1 or (global_hourly != -1 and global_hourly < hourly_limit):
            hourly_limit = global_hourly
            
        if daily_limit == -1 or (global_daily != -1 and global_daily < daily_limit):
            daily_limit = global_daily
        
        # 检查限制
        now = int(time.time())
        
        # 如果没有任何限制，直接通过
        if hourly_limit == -1 and daily_limit == -1:
            return True
            
        # 检查小时限制
        if hourly_limit != -1:
            # 查询一小时内的使用量
            hourly_usage = await self.get_usage(queue, username, 'hourly')
            if hourly_usage >= hourly_limit:
                logger.debug(f"账号 {username} 已达到 {queue} 队列的每小时限制: {hourly_usage}/{hourly_limit}")
                return False
        
        # 检查每日限制
        if daily_limit != -1:
            # 查询24小时内的使用量
            daily_usage = await self.get_usage(queue, username, 'daily')
            if daily_usage >= daily_limit:
                logger.debug(f"账号 {username} 已达到 {queue} 队列的每日限制: {daily_usage}/{daily_limit}")
                return False
                
        # 通过所有限制检查
        return True
        
    async def set_limit(self, queue: str, hourly_limit: int = -1, daily_limit: int = -1, username: str = '*'):
        """设置自定义限制
        
        Args:
            queue: 队列名称
            hourly_limit: 每小时限制(-1表示无限制)
            daily_limit: 每日限制(-1表示无限制)
            username: 特定账号(*表示全局限制)
        """
        await execute(
            self._db_file,
            """INSERT OR REPLACE INTO custom_limits 
               (queue, username, hourly_limit, daily_limit) VALUES (?, ?, ?, ?)""",
            (queue, username, hourly_limit, daily_limit)
        )
        logger.info(f"为 {queue} 队列设置限制 - 账号: {username}, 小时: {hourly_limit}, 每日: {daily_limit}")

    async def get_limit(self, queue: str, username: str = '*'):
        """获取限制设置
        
        Args:
            queue: 队列名称
            username: 账号名(*表示全局限制)
            
        Returns:
            tuple: (hourly_limit, daily_limit)
        """
        # 先尝试获取特定账号的限制
        if username != '*':
            cursor = await fetchone(
                self._db_file,
                "SELECT hourly_limit, daily_limit FROM custom_limits WHERE queue = ? AND username = ?",
                (queue, username)
            )
            if cursor:
                return cursor["hourly_limit"], cursor["daily_limit"]
        
        # 获取全局限制
        cursor = await fetchone(
            self._db_file,
            "SELECT hourly_limit, daily_limit FROM custom_limits WHERE queue = ? AND username = '*'",
            (queue, )
        )
        return (cursor["hourly_limit"], cursor["daily_limit"]) if cursor else (-1, -1)  # 默认无限制
        
    async def record_usage(self, queue: str, username: str, req_count: int = 1):
        """记录队列使用量
        
        Args:
            queue: 队列名称
            username: 账号名
            req_count: 请求次数
        """
        now = int(time.time())
        await execute(
            self._db_file,
            "INSERT INTO usage_stats (queue, username, timestamp, req_count) VALUES (?, ?, ?, ?)",
            (queue, username, now, req_count)
        )
        
    async def get_usage(self, queue: str, username: str, window: str = 'hourly'):
        """查询使用量
        
        Args:
            queue: 队列名称
            username: 账号名
            window: 'hourly'或'daily'
        """
        now = int(time.time())
        # 计算时间窗口开始时间
        if window == 'hourly':
            # 1小时前
            start_time = now - 3600
        elif window == 'daily':
            # 24小时前
            start_time = now - 86400
        else:
            raise ValueError(f"不支持的时间窗口: {window}")
            
        # 查询时间窗口内的使用量
        cursor = await fetchone(
            self._db_file,
            """SELECT SUM(req_count) as total FROM usage_stats 
               WHERE queue = ? AND username = ? AND timestamp >= ?""",
            (queue, username, start_time)
        )
        return cursor["total"] or 0 if cursor else 0
        
    async def cleanup_usage_stats(self, days: int = 7):
        """清理过期的使用记录
        
        Args:
            days: 保留天数
        """
        cutoff = int(time.time()) - (days * 86400)
        await execute(
            self._db_file,
            "DELETE FROM usage_stats WHERE timestamp < ?", 
            (cutoff,)
        )
        logger.info(f"已清理 {days} 天前的使用记录")
        
    async def get_usage_stats(self):
        """获取所有队列的使用统计
        
        Returns:
            dict: 使用统计信息
        """
        stats = {}
        
        # 获取所有队列
        queues = await fetchall(self._db_file, "SELECT DISTINCT queue FROM usage_stats")
        for queue_row in queues:
            queue = queue_row["queue"]
            
            # 获取限制
            global_hourly, global_daily = await self.get_limit(queue)
            
            # 获取账号使用情况
            accounts = []
            acc_list = await fetchall(
                self._db_file,
                "SELECT DISTINCT username FROM usage_stats WHERE queue = ?", 
                (queue,)
            )
            
            for acc_row in acc_list:
                username = acc_row["username"]
                hourly = await self.get_usage(queue, username, 'hourly')
                daily = await self.get_usage(queue, username, 'daily')
                acc_hourly, acc_daily = await self.get_limit(queue, username)
                
                # 计算有效限制(考虑全局限制)
                effective_hourly = acc_hourly
                if effective_hourly == -1 or (global_hourly != -1 and global_hourly < effective_hourly):
                    effective_hourly = global_hourly
                    
                effective_daily = acc_daily
                if effective_daily == -1 or (global_daily != -1 and global_daily < effective_daily):
                    effective_daily = global_daily
                
                accounts.append({
                    "username": username,
                    "hourly_usage": hourly,
                    "daily_usage": daily,
                    "hourly_limit": effective_hourly,
                    "daily_limit": effective_daily
                })
                
            stats[queue] = {
                "global_hourly_limit": global_hourly,
                "global_daily_limit": global_daily,
                "accounts": accounts
            }
            
        return stats

    async def mark_inactive(self, username: str, error_msg: str | None):
        qs = """
        UPDATE accounts SET active = false, error_msg = :error_msg
        WHERE username = :username
        """
        await execute(self._db_file, qs, {"username": username, "error_msg": error_msg})

    async def stats(self):
        def locks_count(queue: str):
            return f"""
            SELECT COUNT(*) FROM accounts
            WHERE json_extract(locks, '$.{queue}') IS NOT NULL
                AND json_extract(locks, '$.{queue}') > datetime('now')
            """

        qs = "SELECT DISTINCT(f.key) as k from accounts, json_each(locks) f"
        rs = await fetchall(self._db_file, qs)
        gql_ops = [x["k"] for x in rs]

        config = [
            ("total", "SELECT COUNT(*) FROM accounts"),
            ("active", "SELECT COUNT(*) FROM accounts WHERE active = true"),
            ("inactive", "SELECT COUNT(*) FROM accounts WHERE active = false"),
            *[(f"locked_{x}", locks_count(x)) for x in gql_ops],
        ]

        qs = f"SELECT {','.join([f'({q}) as {k}' for k, q in config])}"
        rs = await fetchone(self._db_file, qs)
        return dict(rs) if rs else {}

    async def accounts_info(self):
        accounts = await self.get_all()

        items: list[AccountInfo] = []
        for x in accounts:
            item: AccountInfo = {
                "username": x.username,
                "logged_in": (x.headers or {}).get("authorization", "") != "",
                "active": x.active,
                "last_used": x.last_used,
                "total_req": sum(x.stats.values()),
                "error_msg": str(x.error_msg)[0:60],
            }
            items.append(item)

        old_time = datetime(1970, 1, 1).replace(tzinfo=timezone.utc)
        items = sorted(items, key=lambda x: x["username"].lower())
        items = sorted(
            items,
            key=lambda x: x["last_used"] or old_time if x["total_req"] > 0 else old_time,
            reverse=True,
        )
        items = sorted(items, key=lambda x: x["active"], reverse=True)
        # items = sorted(items, key=lambda x: x["total_req"], reverse=True)
        return items

    async def get_for_queue_or_wait(self, queue: str) -> Account | None:
        msg_shown = False
        while True:
            account = await self.get_for_queue(queue)
            if not account:
                if self._raise_when_no_account or get_env_bool("TWS_RAISE_WHEN_NO_ACCOUNT"):
                    raise NoAccountError(f"No account available for queue {queue}")

                if not msg_shown:
                    nat = await self.next_available_at(queue)
                    if not nat:
                        logger.warning("No active accounts. Stopping...")
                        return None

                    msg = f'No account available for queue "{queue}". Next available at {nat}'
                    logger.info(msg)
                    msg_shown = True

                await asyncio.sleep(5)
                continue
            else:
                if msg_shown:
                    logger.info(f"Continuing with account {account.username} on queue {queue}")

            return account

    async def next_available_at(self, queue: str):
        qs = f"""
        SELECT json_extract(locks, '$."{queue}"') as lock_until
        FROM accounts
        WHERE active = true AND json_extract(locks, '$."{queue}"') IS NOT NULL
        ORDER BY lock_until ASC
        LIMIT 1
        """
        rs = await fetchone(self._db_file, qs)
        if rs:
            now, trg = utc.now(), utc.from_iso(rs[0])
            if trg < now:
                return "now"

            at_local = datetime.now() + (trg - now)
            return at_local.strftime("%H:%M:%S")

        return None
