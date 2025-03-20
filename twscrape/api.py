from contextlib import aclosing
from typing import Literal
import hashlib
import mimetypes
import random
import time
import asyncio
from pathlib import Path
from string import ascii_letters
from uuid import uuid1, getnode

from httpx import Response

from .accounts_pool import AccountsPool
from .logger import set_log_level
from .models import Tweet, User, parse_trends, parse_tweet, parse_tweets, parse_user, parse_users
from .queue_client import QueueClient
from .utils import encode_params, find_obj, get_by_path

# 导入媒体上传相关常量
MAX_IMAGE_SIZE = 5_242_880  # ~5 MB
MAX_GIF_SIZE = 15_728_640  # ~15 MB
MAX_VIDEO_SIZE = 536_870_912  # ~530 MB
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
MEDIA_UPLOAD_SUCCEED = 'succeeded'
MEDIA_UPLOAD_FAIL = 'failed'

# 导入DM相关GraphQL操作
OP_SendMessageMutation = "MaxK2PKX1F9Z-9SwqwavTw/useSendMessageMutation"

# OP_{NAME} – {NAME} should be same as second part of GQL ID (required to auto-update script)
OP_SearchTimeline = "U3QTLwGF8sZCHDuWIMSAmg/SearchTimeline"
OP_UserByRestId = "5vdJ5sWkbSRDiiNZvwc2Yg/UserByRestId"
OP_UserByScreenName = "32pL5BWe9WKeSK1MoPvFQQ/UserByScreenName"
OP_TweetDetail = "Ez6kRPyXbqNlhBwcNMpU-Q/TweetDetail"
OP_Followers = "OGScL-RC4DFMsRGOCjPR6g/Followers"
OP_Following = "o5eNLkJb03ayTQa97Cpp7w/Following"
OP_Retweeters = "niCJ2QyTuAgZWv01E7mqJQ/Retweeters"
OP_UserTweets = "Y9WM4Id6UcGFE8Z-hbnixw/UserTweets"
OP_UserTweetsAndReplies = "pZXwh96YGRqmBbbxu7Vk2Q/UserTweetsAndReplies"
OP_ListLatestTweetsTimeline = "H_dAKg97dSn3FOMfrNS8nw/ListLatestTweetsTimeline"
OP_BlueVerifiedFollowers = "WijS8Cwfqhtk5hDN9q7sgw/BlueVerifiedFollowers"
OP_UserCreatorSubscriptions = "H4p-DZU4gYqcZulycazCZw/UserCreatorSubscriptions"
OP_UserMedia = "ophTtKkfXcUKnXlxh9fU5w/UserMedia"
OP_Bookmarks = "1vFR5f4iSCQZLzjdSsNYwA/Bookmarks"
OP_GenericTimelineById = "5u36Lskx1dfACjC_WHmH3Q/GenericTimelineById"

GQL_URL = "https://x.com/i/api/graphql"
GQL_FEATURES = {  # search values here (view source) https://x.com/
    "articles_preview_enabled": False,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_awards_web_tipping_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "tweet_with_visibility_results_prefer_gql_media_interstitial_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "responsive_web_grok_share_attachment_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_image_annotation_enabled": False,
    "responsive_web_grok_analysis_button_from_backend": False,
    "responsive_web_jetfuel_frame": False,
}

KV = dict | None
TrendId = Literal["trending", "news", "sport", "entertainment"] | str


class API:
    # Note: kv is variables, ft is features from original GQL request
    pool: AccountsPool

    def __init__(
        self,
        pool: AccountsPool | str | None = None,
        debug=False,
        proxy: str | None = None,
        raise_when_no_account=False,
    ):
        if isinstance(pool, AccountsPool):
            self.pool = pool
        elif isinstance(pool, str):
            self.pool = AccountsPool(db_file=pool, raise_when_no_account=raise_when_no_account)
        else:
            self.pool = AccountsPool(raise_when_no_account=raise_when_no_account)

        self.proxy = proxy
        self.debug = debug
        if self.debug:
            set_log_level("DEBUG")

    # general helpers

    def _is_end(self, rep: Response, q: str, res: list, cur: str | None, cnt: int, lim: int):
        new_count = len(res)
        new_total = cnt + new_count

        is_res = new_count > 0
        is_cur = cur is not None
        is_lim = lim > 0 and new_total >= lim

        return rep if is_res else None, new_total, is_cur and not is_lim

    def _get_cursor(self, obj: dict, cursor_type="Bottom") -> str | None:
        if cur := find_obj(obj, lambda x: x.get("cursorType") == cursor_type):
            return cur.get("value")
        return None

    # gql helpers

    async def _gql_items(
        self, op: str, kv: dict, ft: dict | None = None, limit=-1, cursor_type="Bottom"
    ):
        queue, cur, cnt, active = op.split("/")[-1], None, 0, True
        kv, ft = {**kv}, {**GQL_FEATURES, **(ft or {})}

        async with QueueClient(self.pool, queue, self.debug, proxy=self.proxy) as client:
            while active:
                params = {"variables": kv, "features": ft}
                if cur is not None:
                    params["variables"]["cursor"] = cur
                if queue in ("SearchTimeline", "ListLatestTweetsTimeline"):
                    params["fieldToggles"] = {"withArticleRichContentState": False}
                if queue in ("UserMedia",):
                    params["fieldToggles"] = {"withArticlePlainText": False}

                rep = await client.get(f"{GQL_URL}/{op}", params=encode_params(params))
                if rep is None:
                    return

                obj = rep.json()
                els = get_by_path(obj, "entries") or []
                els = [
                    x
                    for x in els
                    if not (
                        x["entryId"].startswith("cursor-")
                        or x["entryId"].startswith("messageprompt-")
                    )
                ]
                cur = self._get_cursor(obj, cursor_type)

                rep, cnt, active = self._is_end(rep, queue, els, cur, cnt, limit)
                if rep is None:
                    return

                yield rep

    async def _gql_item(self, op: str, kv: dict, ft: dict | None = None, method: str = "GET"):
        ft = ft or {}
        queue = op.split("/")[-1]
        async with QueueClient(self.pool, queue, self.debug, proxy=self.proxy) as client:
            params = {"variables": {**kv}, "features": {**GQL_FEATURES, **ft}}
            if method == "GET":
                return await client.get(f"{GQL_URL}/{op}", params=encode_params(params))
            elif method == "POST":
                # 直接使用账号的client发送请求，不需要创建新的client
                account = await self.pool.get_for_queue_or_wait(queue)
                if not account:
                    return None
                
                try:
                    # 使用账号的make_client方法确保headers和cookies正确设置
                    client = account.make_client(proxy=self.proxy)
                    
                    # 确保设置正确的headers
                    headers = client.headers.copy()
                    headers["content-type"] = "application/json"
                    
                    # 发送请求
                    rep = await client.post(
                        f"{GQL_URL}/{op}", 
                        json=params,
                        headers=headers
                    )
                    return rep
                finally:
                    await self.pool.unlock(account.username, queue, 1)
                    await client.aclose()
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

    # 媒体上传方法
    async def _upload_media(self, filename: str, is_dm: bool = False, is_profile=False, max_retries=2) -> int | None:
        """
        上传媒体文件到Twitter
        
        Args:
            filename: 文件路径
            is_dm: 是否用于私信
            is_profile: 是否用于个人资料
            max_retries: 最大重试次数
            
        Returns:
            media_id: 上传成功后的媒体ID
        """
        def check_media(category: str, size: int) -> None:
            fmt = lambda x: f'{(x / 1e6):.2f} MB'
            msg = lambda x: f'cannot upload {fmt(size)} {category}, max size is {fmt(x)}'
            if category == 'image' and size > MAX_IMAGE_SIZE:
                raise Exception(msg(MAX_IMAGE_SIZE))
            if category == 'gif' and size > MAX_GIF_SIZE:
                raise Exception(msg(MAX_GIF_SIZE))
            if category == 'video' and size > MAX_VIDEO_SIZE:
                raise Exception(msg(MAX_VIDEO_SIZE))

        # 根据不同用途选择不同的API端点
        url = 'https://upload.twitter.com/1.1/media/upload.json'
        if is_profile:
            url = 'https://upload.twitter.com/i/media/upload.json'

        file = Path(filename)
        if not file.exists():
            if self.debug:
                print(f"文件不存在: {filename}")
            return None
            
        total_bytes = file.stat().st_size
        
        upload_type = 'dm' if is_dm else 'tweet'
        media_type = mimetypes.guess_type(file)[0]
        if not media_type:
            media_type = 'application/octet-stream'  # 默认MIME类型
        media_category = f'{upload_type}_gif' if 'gif' in media_type else f'{upload_type}_{media_type.split("/")[0]}'

        try:
            check_media(media_category, total_bytes)
        except Exception as e:
            if self.debug:
                print(f"媒体检查失败: {e}")
            return None

        # 尝试多个账号
        retries = 0
        last_error = None
        
        while retries <= max_retries:
            # 使用账号池获取一个可用账号
            account = await self.pool.get_for_queue_or_wait("UploadMedia")
            if not account:
                if self.debug:
                    print("没有可用的账号进行媒体上传")
                return None
            
            client = None
            try:
                # 使用账号的make_client方法创建客户端，确保headers和cookies正确设置
                client = account.make_client(proxy=self.proxy)
                
                # 确保设置正确的headers
                headers = client.headers.copy()
                
                # 调试信息
                if self.debug:
                    print(f"尝试使用账号 {account.username} 上传媒体")
                    print(f"文件: {filename}, 大小: {total_bytes}, 类型: {media_type}, 类别: {media_category}")
                
                # INIT 阶段
                params = {
                    'command': 'INIT', 
                    'media_type': media_type, 
                    'total_bytes': total_bytes,
                    'media_category': media_category
                }
                
                rep = await client.post(url=url, headers=headers, params=params)
                
                if rep.status_code >= 400:
                    if self.debug:
                        print(f"INIT 阶段失败: {rep.status_code} {rep.text}")
                    last_error = f"INIT失败: {rep.status_code} - {rep.text}"
                    retries += 1
                    continue

                try:
                    media_id = rep.json().get('media_id')
                    if not media_id:
                        if self.debug:
                            print(f"无法获取media_id, 响应: {rep.text}")
                        last_error = f"无法获取media_id: {rep.text}"
                        retries += 1
                        continue
                except Exception as e:
                    if self.debug:
                        print(f"解析media_id失败: {e}, 响应: {rep.text}")
                    last_error = f"解析失败: {e} - {rep.text}"
                    retries += 1
                    continue

                # APPEND 阶段 - 分块上传
                with open(file, 'rb') as fp:
                    i = 0
                    while chunk := fp.read(UPLOAD_CHUNK_SIZE):
                        params = {'command': 'APPEND', 'media_id': media_id, 'segment_index': i}
                        try:
                            # 尝试两种不同的上传方式
                            try:
                                # 方式1: 使用multipart/form-data格式 (直接发送二进制数据)
                                pad = bytes(''.join(random.choices(ascii_letters, k=16)), encoding='utf-8')
                                data = b''.join([
                                    b'------WebKitFormBoundary',
                                    pad,
                                    b'\r\nContent-Disposition: form-data; name="media"; filename="blob"',
                                    b'\r\nContent-Type: application/octet-stream',
                                    b'\r\n\r\n',
                                    chunk,
                                    b'\r\n------WebKitFormBoundary',
                                    pad,
                                    b'--\r\n',
                                ])
                                _headers = headers.copy()
                                _headers['content-type'] = f'multipart/form-data; boundary=----WebKitFormBoundary{pad.decode()}'
                                rep = await client.post(url=url, headers=_headers, params=params, content=data)
                            except Exception as e:
                                if self.debug:
                                    print(f"尝试第一种上传方式失败，使用备选方式: {e}")
                                # 方式2: 使用files参数 (让httpx自己构造multipart/form-data)
                                files = {'media': chunk}
                                _headers = headers.copy()
                                # 移除content-type让httpx自己设置
                                _headers.pop('content-type', None)
                                rep = await client.post(url=url, headers=_headers, params=params, files=files)
                            
                            if rep.status_code >= 400:
                                if self.debug:
                                    print(f"APPEND段 {i} 失败: {rep.status_code} {rep.text}")
                                raise Exception(f"APPEND失败: {rep.status_code} - {rep.text}")
                        except Exception as e:
                            if self.debug:
                                print(f"上传段 {i} 失败: {e}")
                            last_error = f"段 {i} 上传失败: {e}"
                            raise
                        i += 1
                
                # FINALIZE 阶段
                params = {'command': 'FINALIZE', 'media_id': media_id}
                if is_dm:
                    params |= {'original_md5': hashlib.md5(file.read_bytes()).hexdigest()}
                
                rep = await client.post(url=url, headers=headers, params=params)
                
                if rep.status_code >= 400:
                    if self.debug:
                        print(f"FINALIZE失败: {rep.status_code} {rep.text}")
                    last_error = f"FINALIZE失败: {rep.status_code} - {rep.text}"
                    retries += 1
                    continue

                # 处理视频/GIF等需要处理的媒体
                try:
                    json_data = rep.json()
                    processing_info = json_data.get('processing_info')
                
                    while processing_info:
                        state = processing_info.get('state')
                        if error := processing_info.get("error"):
                            if self.debug:
                                print(f"处理错误: {error}")
                            last_error = f"处理错误: {error}"
                            retries += 1
                            break
                            
                        if state == MEDIA_UPLOAD_SUCCEED:
                            break
                            
                        if state == MEDIA_UPLOAD_FAIL:
                            if self.debug:
                                print(f"处理失败: {processing_info}")
                            last_error = f"处理失败: {processing_info}"
                            retries += 1
                            break
                            
                        check_after_secs = processing_info.get('check_after_secs', random.randint(1, 5))
                        await asyncio.sleep(check_after_secs)
                        
                        params = {'command': 'STATUS', 'media_id': media_id}
                        rep = await client.get(url=url, headers=headers, params=params)
                        
                        if rep.status_code >= 400:
                            if self.debug:
                                print(f"STATUS检查失败: {rep.status_code} {rep.text}")
                            last_error = f"STATUS失败: {rep.status_code} - {rep.text}"
                            retries += 1
                            break
                            
                        processing_info = rep.json().get('processing_info')
                        
                    # 如果完成了处理循环且没有错误，上传成功
                    if not processing_info or (processing_info and processing_info.get('state') == MEDIA_UPLOAD_SUCCEED):
                        if self.debug:
                            print(f"媒体上传成功: {media_id}")
                        return media_id
                    
                except Exception as e:
                    if self.debug:
                        print(f"处理媒体时发生错误: {e}")
                    last_error = f"处理错误: {e}"
                    retries += 1
                    continue
                    
            except Exception as e:
                if self.debug:
                    print(f"上传过程中发生错误: {e}")
                last_error = str(e)
                retries += 1
            finally:
                # 结束时确保释放账号和关闭客户端
                if account:
                    await self.pool.unlock(account.username, "UploadMedia", 1)
                if client:
                    await client.aclose()
        
        # 所有重试都失败了
        if self.debug:
            print(f"上传媒体失败，已尝试 {max_retries + 1} 次。最后错误: {last_error}")
        return None

    # DM发送方法
    async def dm(self, text: str, receivers: list[int], media: str = None, wait_for_account=False) -> dict:
        """
        发送私信给指定用户
        
        Args:
            text: 私信文本内容
            receivers: 接收者用户ID列表
            media: 媒体文件路径(可选)
            wait_for_account: 如果没有可用账号，是否等待账号可用
            
        Returns:
            响应数据
        """
        if not receivers:
            return {"error": "接收者列表不能为空"}
            
        # 准备GraphQL变量
        variables = {
            "message": {},
            "requestId": str(uuid1(getnode())),
            "target": {"participant_ids": receivers},
        }
        
        # 获取队列名
        queue = OP_SendMessageMutation.split("/")[-1]
        
        # 获取一个可用账号
        if wait_for_account:
            account = await self.pool.get_for_queue_or_wait(queue)
        else:
            account = await self.pool.get_for_queue(queue)
        
        if not account:
            # 获取下一个可用时间
            next_available = await self.pool.next_available_at(queue)
            if next_available:
                return {
                    "error": f"没有可用的账号发送私信，所有账号都在冷却中。下一个账号将在 {next_available} 可用。",
                    "next_available": next_available
                }
            else:
                return {"error": "没有可用的账号发送私信，请添加更多账号。"}
                
        print(f"Selected Account: {account.username}")

        client = None
        try:
            # 处理媒体上传
            if media:
                if self.debug:
                    print(f"开始上传媒体: {media}")
                    
                media_id = await self._upload_media(media, is_dm=True)
                if media_id:
                    if self.debug:
                        print(f"媒体上传成功，ID: {media_id}")
                    variables['message']['media'] = {'id': media_id, 'text': text}
                else:
                    # 媒体上传失败，仅发送文本
                    if self.debug:
                        print("媒体上传失败，将只发送文本消息")
                    variables['message']['text'] = {'text': text}
            else:
                variables['message']['text'] = {'text': text}
            
            # 使用账号的client发送请求
            client = account.make_client(proxy=self.proxy)
            
            # 构建GraphQL请求参数
            params = {
                "variables": variables,
                "features": GQL_FEATURES
            }
            
            # 确保设置正确的headers
            headers = client.headers.copy()
            headers["content-type"] = "application/json"
            
            if self.debug:
                print(f"正在使用GraphQL API发送私信")
                
            # 直接发送POST请求
            res = await client.post(
                f"{GQL_URL}/{OP_SendMessageMutation}", 
                json=params,
                headers=headers
            )
            
            if not res or res.status_code >= 400:
                error_msg = f"请求失败: {res.status_code if res else 'No response'}"
                if res:
                    error_msg += f" - {res.text}"
                if self.debug:
                    print(error_msg)
                return {"error": error_msg}
                
            response_data = res.json()
            
            # 检查错误
            if "errors" in response_data:
                errors = response_data["errors"]
                error_messages = "; ".join([f"({e.get('code', 'unknown')}) {e.get('message', 'Unknown error')}" for e in errors])
                if self.debug:
                    print(f"发送私信失败: {error_messages}")
                return {"error": f"发送私信失败: {error_messages}", "raw_response": response_data}
                
            # 检查成功
            if self.debug:
                print("私信发送成功")
            return response_data
            
        except Exception as e:
            if self.debug:
                print(f"发送私信时发生异常: {e}")
            return {"error": f"发送私信时发生异常: {str(e)}"}
        finally:
            # 结束时确保释放账号和关闭客户端
            if account:
                await self.pool.unlock(account.username, queue, 1)
            if client:
                await client.aclose()

    async def _add_alt_text(self, media_id: int, text: str):
        """为媒体添加替代文本(Alt Text)"""
        params = {"media_id": media_id, "alt_text": {"text": text}}
        url = 'https://api.twitter.com/1.1/media/metadata/create.json'
        
        # 获取一个可用账号
        account = await self.pool.get_for_queue_or_wait("AltText")
        if not account:
            return None
        
        client = None
        try:
            # 使用账号的make_client方法创建客户端
            client = account.make_client(proxy=self.proxy)
            
            # 确保设置正确的headers
            headers = client.headers.copy()
            headers['content-type'] = 'application/json'
            
            # 发送请求
            return await client.post(url, headers=headers, json=params)
        finally:
            # 结束时确保释放账号和关闭客户端
            if account:
                await self.pool.unlock(account.username, "AltText", 1)
            if client:
                await client.aclose()

    # search

    async def search_raw(self, q: str, limit=-1, kv: KV = None):
        op = OP_SearchTimeline
        kv = {
            "rawQuery": q,
            "count": 20,
            "product": "Latest",
            "querySource": "typed_query",
            **(kv or {}),
        }
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def search(self, q: str, limit=-1, kv: KV = None):
        async with aclosing(self.search_raw(q, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    yield x

    async def search_user(self, q: str, limit=-1, kv: KV = None):
        kv = {"product": "People", **(kv or {})}
        async with aclosing(self.search_raw(q, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # user_by_id

    async def user_by_id_raw(self, uid: int, kv: KV = None):
        op = OP_UserByRestId
        kv = {"userId": str(uid), "withSafetyModeUserFields": True, **(kv or {})}
        ft = {
            "hidden_profile_likes_enabled": True,
            "highlights_tweets_tab_ui_enabled": True,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "hidden_profile_subscriptions_enabled": True,
            "responsive_web_twitter_article_notes_tab_enabled": False,
            "subscriptions_feature_can_gift_premium": False,
            "profile_label_improvements_pcf_label_in_post_enabled": False,
        }
        return await self._gql_item(op, kv, ft)

    async def user_by_id(self, uid: int, kv: KV = None) -> User | None:
        rep = await self.user_by_id_raw(uid, kv=kv)
        return parse_user(rep) if rep else None

    # user_by_login

    async def user_by_login_raw(self, login: str, kv: KV = None):
        op = OP_UserByScreenName
        kv = {"screen_name": login, "withSafetyModeUserFields": True, **(kv or {})}
        ft = {
            "highlights_tweets_tab_ui_enabled": True,
            "hidden_profile_likes_enabled": True,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "hidden_profile_subscriptions_enabled": True,
            "subscriptions_verification_info_verified_since_enabled": True,
            "subscriptions_verification_info_is_identity_verified_enabled": False,
            "responsive_web_twitter_article_notes_tab_enabled": False,
            "subscriptions_feature_can_gift_premium": False,
            "profile_label_improvements_pcf_label_in_post_enabled": False,
        }
        return await self._gql_item(op, kv, ft)

    async def user_by_login(self, login: str, kv: KV = None) -> User | None:
        rep = await self.user_by_login_raw(login, kv=kv)
        return parse_user(rep) if rep else None

    # tweet_details

    async def tweet_details_raw(self, twid: int, kv: KV = None):
        op = OP_TweetDetail
        kv = {
            "focalTweetId": str(twid),
            "with_rux_injections": True,
            "includePromotedContent": True,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        return await self._gql_item(op, kv)

    async def tweet_details(self, twid: int, kv: KV = None) -> Tweet | None:
        rep = await self.tweet_details_raw(twid, kv=kv)
        return parse_tweet(rep, twid) if rep else None

    # tweet_replies
    # note: uses same op as tweet_details, see: https://github.com/vladkens/twscrape/issues/104

    async def tweet_replies_raw(self, twid: int, limit=-1, kv: KV = None):
        op = OP_TweetDetail
        kv = {
            "focalTweetId": str(twid),
            "referrer": "tweet",
            "with_rux_injections": True,
            "includePromotedContent": True,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        async with aclosing(
            self._gql_items(op, kv, limit=limit, cursor_type="ShowMoreThreads")
        ) as gen:
            async for x in gen:
                yield x

    async def tweet_replies(self, twid: int, limit=-1, kv: KV = None):
        async with aclosing(self.tweet_replies_raw(twid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    if x.inReplyToTweetId == twid:
                        yield x

    # followers

    async def followers_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_Followers
        kv = {"userId": str(uid), "count": 20, "includePromotedContent": False, **(kv or {})}
        ft = {"responsive_web_twitter_article_notes_tab_enabled": False}
        async with aclosing(self._gql_items(op, kv, limit=limit, ft=ft)) as gen:
            async for x in gen:
                yield x

    async def followers(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.followers_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # verified_followers

    async def verified_followers_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_BlueVerifiedFollowers
        kv = {"userId": str(uid), "count": 20, "includePromotedContent": False, **(kv or {})}
        ft = {
            "responsive_web_twitter_article_notes_tab_enabled": True,
        }
        async with aclosing(self._gql_items(op, kv, limit=limit, ft=ft)) as gen:
            async for x in gen:
                yield x

    async def verified_followers(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.verified_followers_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # following

    async def following_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_Following
        kv = {"userId": str(uid), "count": 20, "includePromotedContent": False, **(kv or {})}
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def following(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.following_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # subscriptions

    async def subscriptions_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_UserCreatorSubscriptions
        kv = {"userId": str(uid), "count": 20, "includePromotedContent": False, **(kv or {})}
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def subscriptions(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.subscriptions_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # retweeters

    async def retweeters_raw(self, twid: int, limit=-1, kv: KV = None):
        op = OP_Retweeters
        kv = {"tweetId": str(twid), "count": 20, "includePromotedContent": True, **(kv or {})}
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def retweeters(self, twid: int, limit=-1, kv: KV = None):
        async with aclosing(self.retweeters_raw(twid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_users(rep.json(), limit):
                    yield x

    # user_tweets

    async def user_tweets_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_UserTweets
        kv = {
            "userId": str(uid),
            "count": 40,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def user_tweets(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.user_tweets_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    yield x

    # user_tweets_and_replies

    async def user_tweets_and_replies_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_UserTweetsAndReplies
        kv = {
            "userId": str(uid),
            "count": 40,
            "includePromotedContent": True,
            "withCommunity": True,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def user_tweets_and_replies(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.user_tweets_and_replies_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    yield x

    # user_media

    async def user_media_raw(self, uid: int, limit=-1, kv: KV = None):
        op = OP_UserMedia
        kv = {
            "userId": str(uid),
            "count": 40,
            "includePromotedContent": False,
            "withClientEventToken": False,
            "withBirdwatchNotes": False,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }

        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def user_media(self, uid: int, limit=-1, kv: KV = None):
        async with aclosing(self.user_media_raw(uid, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep, limit):
                    # sometimes some tweets without media, so skip them
                    media_count = (
                        len(x.media.photos) + len(x.media.videos) + len(x.media.animated)
                        if x.media
                        else 0
                    )

                    if media_count > 0:
                        yield x

    # list_timeline

    async def list_timeline_raw(self, list_id: int, limit=-1, kv: KV = None):
        op = OP_ListLatestTweetsTimeline
        kv = {"listId": str(list_id), "count": 20, **(kv or {})}
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def list_timeline(self, list_id: int, limit=-1, kv: KV = None):
        async with aclosing(self.list_timeline_raw(list_id, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep, limit):
                    yield x

    # trends

    async def trends_raw(self, trend_id: TrendId, limit=-1, kv: KV = None):
        map = {
            "trending": "VGltZWxpbmU6DAC2CwABAAAACHRyZW5kaW5nAAA",
            "news": "VGltZWxpbmU6DAC2CwABAAAABG5ld3MAAA",
            "sport": "VGltZWxpbmU6DAC2CwABAAAABnNwb3J0cwAA",
            "entertainment": "VGltZWxpbmU6DAC2CwABAAAADWVudGVydGFpbm1lbnQAAA",
        }
        trend_id = map.get(trend_id, trend_id)

        op = OP_GenericTimelineById
        kv = {
            "timelineId": trend_id,
            "count": 20,
            "withQuickPromoteEligibilityTweetFields": True,
            **(kv or {}),
        }
        async with aclosing(self._gql_items(op, kv, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def trends(self, trend_id: TrendId, limit=-1, kv: KV = None):
        async with aclosing(self.trends_raw(trend_id, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_trends(rep, limit):
                    yield x

    async def search_trend(self, q: str, limit=-1, kv: KV = None):
        kv = {
            "querySource": "trend_click",
            **(kv or {}),
        }
        async with aclosing(self.search_raw(q, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    yield x

    # Get current user bookmarks

    async def bookmarks_raw(self, limit=-1, kv: KV = None):
        op = OP_Bookmarks
        kv = {
            "count": 20,
            "includePromotedContent": False,
            "withClientEventToken": False,
            "withBirdwatchNotes": False,
            "withVoice": True,
            "withV2Timeline": True,
            **(kv or {}),
        }
        ft = {
            "graphql_timeline_v2_bookmark_timeline": True,
        }
        async with aclosing(self._gql_items(op, kv, ft, limit=limit)) as gen:
            async for x in gen:
                yield x

    async def bookmarks(self, limit=-1, kv: KV = None):
        async with aclosing(self.bookmarks_raw(limit=limit, kv=kv)) as gen:
            async for rep in gen:
                for x in parse_tweets(rep.json(), limit):
                    yield x

    # 添加在末尾
    async def account_status(self) -> dict:
        """
        获取账号池中所有账号的状态信息
        
        Returns:
            包含账号状态信息的字典
        """
        result = {
            "total": 0,
            "active": 0,
            "inactive": 0,
            "locks": {},
            "accounts": []
        }
        
        # 从数据库获取所有账号
        accounts = await self.pool.get_all_accounts()
        result["total"] = len(accounts)
        
        # 获取所有账号锁信息
        locks = await self.pool.get_all_locks()
        
        for account in accounts:
            # 账号是否激活
            is_active = account.get("is_active", True)
            if is_active:
                result["active"] += 1
            else:
                result["inactive"] += 1
                
            # 获取账号的锁信息
            account_locks = {}
            for lock in locks:
                if lock.get("username") == account.get("username"):
                    queue = lock.get("queue")
                    account_locks[queue] = {
                        "locked_until": lock.get("locked_until"),
                        "remaining_seconds": max(0, lock.get("locked_until", 0) - time.time())
                    }
                    
                    # 汇总锁信息
                    if queue not in result["locks"]:
                        result["locks"][queue] = 0
                    result["locks"][queue] += 1
            
            # 添加账号信息
            result["accounts"].append({
                "username": account.get("username"),
                "is_active": is_active,
                "inactive_reason": account.get("inactive_reason"),
                "locks": account_locks
            })
            
        return result
        
    async def reset_locks(self, queue: str = None):
        """
        重置所有队列的锁
        
        Returns:
            操作结果
        """
        # 注意：AccountsPool.reset_locks()不接受参数
        return await self.pool.reset_locks()
