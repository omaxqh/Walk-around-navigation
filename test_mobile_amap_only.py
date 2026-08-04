import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

_TEST_DATA_DIR = tempfile.TemporaryDirectory()
os.environ["ROUTESNAP_DATA_DIR"] = _TEST_DATA_DIR.name

import app
import poi_disambiguate


class MobileAmapOnlyTest(unittest.TestCase):
    def setUp(self):
        self._original_access_token = app.ROUTESNAP_ACCESS_TOKEN
        app.ROUTESNAP_ACCESS_TOKEN = "test-access-token-at-least-32-characters"
        self.auth_headers = {"Authorization": "Bearer test-access-token-at-least-32-characters"}

    def tearDown(self):
        app.ROUTESNAP_ACCESS_TOKEN = self._original_access_token

    def test_multi_stop_url_uses_native_amap_scheme(self):
        locations = [
            {"name": "起点", "lat": 30.1, "lon": 120.1},
            {"name": "途经点", "lat": 30.2, "lon": 120.2},
            {"name": "终点", "lat": 30.3, "lon": 120.3},
        ]

        url = app.build_amap_url(locations, mode=2)

        self.assertTrue(url.startswith("iosamap://path?"))
        self.assertIn("vian=1", url)
        self.assertIn("vialons=120.2", url)
        self.assertIn("vialats=30.2", url)

    def test_active_backend_has_no_web_route_fallback(self):
        source = Path(app.__file__).read_text(encoding="utf-8")

        self.assertNotIn("frogomap.pages.dev/route", source)
        self.assertNotIn("build_route_web_url", source)
        self.assertNotIn("native_amap_url", source)

    def test_llm_defaults_to_current_deepseek_api(self):
        source = Path(app.__file__).read_text(encoding="utf-8")

        self.assertEqual(
            app.DEEPSEEK_API_URL, "https://api.deepseek.com/chat/completions"
        )
        self.assertEqual(app.DEEPSEEK_MODEL, "deepseek-v4-flash")
        self.assertNotIn("modelservice.jdcloud.com", source)
        self.assertNotIn("Kimi-K2-Turbo", source)
        self.assertNotIn('DEEPSEEK_API_KEY = "sk-', source)

    def test_xhs_pin_route_is_parsed_without_llm(self):
        text = """西湖春天顶流赏花点
✍️Route·路线推荐
📍玛瑙寺-📍苏堤（北山路入口）-📍曲院风荷-📍苏堤-📍花港观鱼-📍太子湾公园-📍杨公堤-📍西湖国宾馆-📍茅家埠
⭐️行程亮点
"""

        result = app.try_fast_parse(text)

        self.assertIsNotNone(result)
        self.assertEqual(
            result["routes"][0]["points"],
            [
                "玛瑙寺",
                "苏堤北山路入口",
                "曲院风荷",
                "苏堤",
                "花港观鱼",
                "太子湾公园",
                "杨公堤",
                "西湖国宾馆",
                "茅家埠",
            ],
        )

    def test_extract_xhs_link_supports_cn_and_com_shortlinks(self):
        self.assertEqual(
            app.extract_xhs_link("看看 http://xhslink.cn/o/9ifdgm0MtJf 复制口令"),
            "http://xhslink.cn/o/9ifdgm0MtJf",
        )
        self.assertEqual(
            app.extract_xhs_link("看看 https://xhslink.com/a/Abc123?x=1。"),
            "https://xhslink.com/a/Abc123?x=1",
        )

    def test_xhs_fetch_retries_with_desktop_user_agent(self):
        error_response = Mock(
            status_code=200,
            url="https://www.xiaohongshu.com/website-login/error?error_code=300011",
            text="error",
        )
        content_response = Mock(
            status_code=200,
            url="https://www.xiaohongshu.com/discovery/item/test",
            text='"title":"西湖路线","x":1,"desc":"玛瑙寺 → 曲院风荷 → 白塔公园","x":2',
        )
        session = Mock()
        session.get.side_effect = [error_response, content_response]

        with patch.object(app.requests, "Session", return_value=session):
            result = app.fetch_xhs_content("http://xhslink.cn/o/test")

        self.assertTrue(result["success"])
        self.assertEqual(result["strategy"], "desktop")
        self.assertEqual(session.get.call_count, 2)
        primary_headers = session.get.call_args_list[0].kwargs["headers"]
        fallback_headers = session.get.call_args_list[1].kwargs["headers"]
        self.assertIn("iPhone OS 18_6", primary_headers["User-Agent"])
        self.assertIn("Chrome/138", fallback_headers["User-Agent"])

    def test_client_source_html_is_converted_to_full_post_text(self):
        html = (
            '<html><script>window.__INITIAL_STATE__={'
            '"title":"西湖路线","x":1,'
            '"desc":"玛瑙寺 → 曲院风荷 → 白塔公园","x":2'
            '}</script></html>'
        )
        result = app._normalize_client_source_text(
            html, "http://xhslink.cn/o/test"
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["strategy"], "client_source_html")
        self.assertIn("玛瑙寺", result["full_text"])
        self.assertIn("白塔公园", result["full_text"])

    def test_fetch_failure_never_calls_ai_or_guesses_from_title(self):
        client = app.app.test_client()
        failure = {
            "success": False,
            "error_code": "xhs_fetch_failed",
            "upstream_code": "300011",
            "retryable": True,
        }

        with (
            patch.object(app, "_get_route_cache", return_value=None),
            patch.object(app, "fetch_xhs_content", return_value=failure),
            patch.object(app, "extract_route_with_ai") as ai,
        ):
            response = client.post(
                "/parse",
                json={
                    "text": "超全西湖一日赏樱攻略... http://xhslink.cn/o/9ifdgm0MtJf",
                    "mode": 2,
                },
                headers=self.auth_headers,
            )

        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "xhs_fetch_failed")
        self.assertEqual(payload["upstream_code"], "300011")
        self.assertNotIn("routes", payload)
        ai.assert_not_called()

    def test_xhs_route_cache_is_checked_before_fetch(self):
        cached = {
            "success": True,
            "content_type": "single_route",
            "route_count": 1,
            "routes": [{
                "points": ["玛瑙寺", "白塔公园"],
                "amap_url": "iosamap://path?dev=0&t=2",
            }],
        }
        client = app.app.test_client()
        with (
            patch.object(app, "_get_route_cache", return_value=cached),
            patch.object(app, "fetch_xhs_content") as fetch,
        ):
            response = client.post(
                "/parse",
                json={"text": "http://xhslink.cn/o/9ifdgm0MtJf", "mode": 2},
                headers=self.auth_headers,
            )

        self.assertEqual(response.get_json()["routes"][0]["points"], ["玛瑙寺", "白塔公园"])
        fetch.assert_not_called()

    def test_poi_list_cluster_cannot_override_requested_walk_mode(self):
        route_info = {
            "is_route": True,
            "content_type": "poi_list",
            "city": "杭州",
            "routes": [{"name": "", "points": ["甲公园", "乙公园"]}],
        }
        disambiguation = [
            {"status": "auto_accept", "confidence": 1, "selected_poi": {"name": "甲公园", "lat": 30.1, "lon": 120.1}},
            {"status": "auto_accept", "confidence": 1, "selected_poi": {"name": "乙公园", "lat": 30.3, "lon": 120.3}},
        ]
        drive_cluster = [{
            "mode": "drive",
            "pois": [
                {"name": "甲公园", "location": "120.1,30.1"},
                {"name": "乙公园", "location": "120.3,30.3"},
            ],
        }]
        client = app.app.test_client()
        with (
            patch.object(app, "_get_route_cache", return_value=None),
            patch.object(app, "_set_route_cache"),
            patch.object(app, "try_fast_parse", return_value=route_info),
            patch.object(app, "disambiguate_route", return_value=disambiguation),
            patch.object(app, "cluster_pois_by_distance", return_value=drive_cluster),
        ):
            payload = client.post(
                "/parse",
                json={"text": "两个公园推荐", "mode": 2},
                headers=self.auth_headers,
            ).get_json()

        self.assertEqual(payload["routes"][0]["mode"], "walk")
        self.assertIn("&t=2", payload["routes"][0]["amap_url"])

    def test_share_edition_rejects_missing_access_token(self):
        client = app.app.test_client()

        response = client.post("/parse", json={"text": "测试路线"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "unauthorized")

    def test_share_edition_rejects_short_server_token(self):
        client = app.app.test_client()
        with patch.object(app, "ROUTESNAP_ACCESS_TOKEN", "too-short"):
            response = client.post("/parse", json={"text": "测试路线"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error_code"], "server_not_configured")

    def test_share_edition_health_is_public(self):
        client = app.app.test_client()

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("DEEPSEEK_API_KEY", response.get_data(as_text=True))

    def test_runtime_files_use_configured_data_directory(self):
        self.assertEqual(Path(app._ROUTE_CACHE_DB).parent, Path(_TEST_DATA_DIR.name))
        self.assertEqual(Path(poi_disambiguate._CACHE_DB).parent, Path(_TEST_DATA_DIR.name))

    def test_generic_city_name_is_not_a_poi(self):
        self.assertTrue(app._is_generic_city_token("杭州", "杭州"))
        self.assertTrue(app._is_generic_city_token("北京市"))
        self.assertFalse(app._is_generic_city_token("杭州植物园", "杭州"))

    def test_route_cache_persists_after_memory_is_cleared(self):
        response = {
            "success": True,
            "routes": [{
                "points": ["玛瑙寺", "白塔公园"],
                "amap_url": "iosamap://path?dev=0&t=2",
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = str(Path(temp_dir) / "cache.db")
            with patch.object(app, "_ROUTE_CACHE_DB", cache_path):
                app._persistent_route_cache_set("route:test", response)
                app._route_cache.clear()
                self.assertEqual(
                    app._persistent_route_cache_get("route:test"), response
                )

    def test_benchmark_body_fast_parses_all_ten_points(self):
        text = """超全西湖一日赏樱攻略
路线：玛瑙寺 ➠ 曲院风荷 ➠ 云松书舍 ➠ 黛色参天 ➠ 中国茶叶博物馆 ➠ 乌龟潭 ➠ 花港公园 ➠ 太子湾公园 ➠ 杭州少年儿童公园 ➠ 白塔公园
"""
        result = app.try_fast_parse(app.normalize_connectors(text))
        self.assertEqual(
            result["routes"][0]["points"],
            ["玛瑙寺", "曲院风荷", "云松书舍", "黛色参天", "中国茶叶博物馆", "乌龟潭", "花港公园", "太子湾公园", "杭州少年儿童公园", "白塔公园"],
        )

    def test_descriptive_route_prefix_keeps_quyuanfenghe_start(self):
        text = """🌸接下来的杭州…是万万次的春和景明！
P1：太子湾公园
P14：西湖曲院风荷·曲渡清波
🗺️西湖一日city walk赏花路线：曲苑风荷·九渡清波→杭州花圃→杨公堤→太子湾公园→茅家埠→浴鹄湾→乌龟潭（全程约10公里）
"""

        result = app.try_fast_parse(text)

        self.assertEqual(
            result["routes"][0]["points"],
            ["曲院风荷", "杭州花圃", "杨公堤", "太子湾公园", "茅家埠", "浴鹄湾", "乌龟潭"],
        )

    def test_yanggongdi_is_not_pinned_to_stale_hot_poi(self):
        hot_pois = Path(app.__file__).with_name("config").joinpath("hot_pois.json").read_text(encoding="utf-8")

        self.assertNotIn('"杨公堤": {', hot_pois)

    def test_five_kilometer_silent_filter_is_preserved(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn("if min_dist <= 5", source)
        self.assertIn("else: 离群点，静默丢弃", source)


if __name__ == "__main__":
    unittest.main()
