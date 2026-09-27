import unittest
import zipfile
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from document_pipeline import extract_text
from notice_analysis import analyze_notice, normalize_agent_analysis, normalize_agent_brief
from notice_viewer import (
    DEFAULT_CONFIG,
    NoticeSpider,
    NoticeStore,
    credential_status,
    in_date_range,
    normalize_date,
    parse_detail_html,
    safe_filename,
)


class NoticeViewerTests(unittest.TestCase):
    def test_normalize_date(self):
        self.assertEqual(normalize_date("发布时间：2026/07/10"), "2026-07-10")
        self.assertEqual(normalize_date("2026年7月10日 09:05"), "2026-07-10 09:05")

    def test_safe_filename(self):
        self.assertEqual(safe_filename("a:b?.pdf"), "a_b_.pdf")
        self.assertEqual(safe_filename(""), "附件")

    def test_notice_detail_uses_school_selectors_and_removes_noise(self):
        source = """
        <html><head><title>无关页面标题</title></head><body>
        <div class="layui-header">个人中心 退出登录</div>
        <h1 class="layui-custom-h1">关于测试通知</h1>
        <div class="layui-custom-h2">发布时间：2026/07/10 部门：教务处</div>
        <div class="layui-card"><div class="layui-card-body">
          这是通知正文第一段。<br>这是通知正文第二段。
          <a href="/uc/notice/1/download">测试附件.docx</a>
        </div></div>
        </body></html>
        """
        result = parse_detail_html(source, "http://example.test/notice", {"id": "1", "type": "notice", "title": "列表标题"})
        self.assertEqual(result["title"], "关于测试通知")
        self.assertEqual(result["published_at"], "2026-07-10")
        self.assertEqual(result["department"], "教务处")
        self.assertNotIn("个人中心", result["content"])
        self.assertEqual(len(result["attachments"]), 1)

    def test_fw_list_keeps_static_pdf_url(self):
        source = """
        <a class="layui-custom-card-li" href="/uc/static/file/pdf/20260924/val/test.pdf">
          <div class="layui-col-xs3">苏大发文</div>
          <div class="layui-col-xs6">关于测试发文的通知</div>
          <div class="layui-col-xs3">2026/09/24</div>
        </a>
        """
        spider = NoticeSpider(dict(DEFAULT_CONFIG))
        spider._get = lambda _url: SimpleNamespace(content=source.encode("utf-8"))

        result = spider.fetch_list("fw")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "关于测试发文的通知")
        self.assertEqual(result[0]["url"], "http://lb.tec.suda.edu.cn/uc/static/file/pdf/20260924/val/test.pdf")

    def test_xngs_list_supports_pub_entity(self):
        source = """
        <a class="layui-custom-card-li" href="/uc/pub/10102/entity" title="关于测试公示">
          <div class="layui-col-xs12 layui-col-sm9 layui-col-md9">关于测试公示</div>
          <div class="layui-col-xs6 layui-col-sm3 layui-col-md3">2026/09/26</div>
        </a>
        """
        spider = NoticeSpider(dict(DEFAULT_CONFIG))
        spider._get = lambda _url: SimpleNamespace(content=source.encode("utf-8"))

        result = spider.fetch_list("xngs")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "10102")
        self.assertEqual(result[0]["date"], "2026/09/26")
        self.assertEqual(result[0]["url"], "http://lb.tec.suda.edu.cn/uc/pub/10102/entity")

    def test_notice_analysis_categorizes_competition_and_advises(self):
        result = analyze_notice({"title": "关于参加创新创业大赛报名的通知", "content": "报名截止：2026年10月1日"})

        self.assertEqual(result["primary_category"], "competition")
        self.assertIn("competition", result["categories"])
        self.assertIn("报名截止", result["advice"])

    def test_date_scope_and_agent_analysis_are_bounded(self):
        self.assertTrue(in_date_range("2026/08/31", "2026-08-31", "2026-09-27"))
        self.assertFalse(in_date_range("2026/08/30", "2026-08-31", "2026-09-27"))

        result = normalize_agent_analysis({"categories": ["competition", "unknown"], "confidence": 2, "source": "agent:test"})

        self.assertEqual(result["categories"], ["competition"])
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["source"], "agent:test")

    def test_normalize_agent_brief_keeps_evidence_and_status(self):
        result = normalize_agent_brief(
            {
                "summary": "面向学生征集报名材料。",
                "key_points": ["提交报名表", "按时参加审核"],
                "deadlines": [{"date": "2026-10-01", "event": "报名截止", "evidence": "报名截止：2026年10月1日"}],
                "status": "complete",
                "confidence": 0.92,
                "source": "opencode:test",
            }
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["deadlines"][0]["date"], "2026-10-01")
        self.assertEqual(result["deadlines"][0]["evidence"], "报名截止：2026年10月1日")
        self.assertEqual(result["source"], "opencode:test")

    def test_extract_text_supports_plain_text_and_docx(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            text_path = root / "notice.txt"
            text_path.write_text("第一行\n第二行", encoding="utf-8")
            text_result = extract_text(text_path)
            self.assertEqual(text_result["status"], "TEXT_EXTRACTED")
            self.assertIn("第二行", text_result["text"])

            docx_path = root / "notice.docx"
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>DOCX正文</w:t></w:r></w:p></w:body></w:document>'
            )
            with zipfile.ZipFile(docx_path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
            docx_result = extract_text(docx_path)
            self.assertEqual(docx_result["status"], "TEXT_EXTRACTED")
            self.assertIn("DOCX正文", docx_result["text"])

    def test_credential_status_is_masked_and_supports_external_file(self):
        with TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            credentials_path.write_text(json.dumps({"username": "2416486001", "password": "secret-value"}), encoding="utf-8")
            with patch.dict(os.environ, {"TEC_USERNAME": "", "TEC_PASSWORD": ""}):
                result = credential_status({"credentials_file": str(credentials_path)})

            self.assertEqual(result["status"], "READY")
            self.assertEqual(result["username"], "24***01")
            self.assertEqual(result["username_source"], "credentials_file")
            self.assertTrue(result["password_configured"])
            self.assertNotIn("secret-value", json.dumps(result))

    def test_store_deduplicates_and_supports_fuzzy_search(self):
        item = {
            "id": "42",
            "type": "notice",
            "title": "关于创新创业比赛报名的通知",
            "department": "学生事务部",
            "date": "2026/09/20",
            "published_text": "2026/09/20",
            "published_at": "2026-09-20",
            "url": "http://example.test/uc/notice/42/entity",
            "content": "请在截止日期前提交报名材料。",
            "attachments": [],
        }
        with TemporaryDirectory() as temp_dir:
            store = NoticeStore(Path(temp_dir) / "notices.sqlite3")
            self.assertEqual(store.upsert(item), (True, False))
            self.assertEqual(store.upsert(item), (False, False))
            store.save_brief(
                "notice:42",
                normalize_agent_brief({"summary": "请在截止日前提交。", "status": "complete"}),
            )
            self.assertEqual(len(store.list_notices(q="创业")), 1)
            self.assertEqual(store.list_notices(q="创业")[0]["brief"]["summary"], "请在截止日前提交。")
            self.assertEqual(len(store.list_notices(q="不存在")), 0)
            self.assertEqual(len(store.list_notices(since="2026-09-21")), 0)
            detail = store.get_notice_detail("notice:42")
            self.assertIsNotNone(detail)
            self.assertEqual(detail["title"], item["title"])

    def test_store_supports_offset_pagination(self):
        with TemporaryDirectory() as temp_dir:
            store = NoticeStore(Path(temp_dir) / "notices.sqlite3")
            for number in range(3):
                item = {
                    "id": str(number),
                    "type": "notice",
                    "title": f"分页通知{number}",
                    "department": "测试部门",
                    "date": f"2026/09/{20 + number}",
                    "published_text": f"2026/09/{20 + number}",
                    "published_at": f"2026-09-{20 + number}",
                    "url": f"http://example.test/uc/notice/{number}/entity",
                    "content": "分页测试正文",
                    "attachments": [],
                }
                store.upsert(item)

            first_page = store.list_notices(limit=2, offset=0)
            second_page = store.list_notices(limit=2, offset=2)
            self.assertEqual(len(first_page), 2)
            self.assertEqual(len(second_page), 1)
            self.assertNotEqual(first_page[0]["notice_key"], second_page[0]["notice_key"])

    def test_fetch_list_respects_per_type_limit(self):
        source = "".join(
            f'<a class="layui-custom-card-li" href="/uc/pub/{number}/entity" title="公示{number}">'
            f'<div class="layui-col-md9">公示{number}</div><div class="layui-col-md3">2026/09/2{number}</div></a>'
            for number in (1, 2, 3)
        )
        spider = NoticeSpider(dict(DEFAULT_CONFIG))
        spider._get = lambda _url: SimpleNamespace(content=source.encode("utf-8"))

        result = spider.fetch_list("xngs", max_items=2)

        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
