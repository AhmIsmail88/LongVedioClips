import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.titles import valid_title
from utils.sanitize import load_blocklist, mask_text, contains_blocked_text
from core.smart_reframe import _verified_faces, _choose_person, _crop_contains

class SafeTitlesFramingTests(unittest.TestCase):
    def test_titles(self):
        self.assertTrue(valid_title("كيف تبني بيتا سعيدا"))
        for title in ("Clip 01", "Part 1", "مقطع ٠١", "sexual secrets", "عنوان إباحي"):
            self.assertFalse(valid_title(title),title)

    def test_mask_obfuscation(self):
        for text in ("hello,porn", "p.o.r.n", "p\u200born", "وإباحية", "sexual", "كلام إباحي"):
            result=mask_text(text,load_blocklist())
            self.assertIn("*",result,text)
            self.assertFalse(contains_blocked_text(result),result)

    def test_innocent_words(self):
        text="بزاف الناس وكسرة خبز"
        self.assertEqual(mask_text(text,load_blocklist()),text)

    def test_poster_face_rejected(self):
        people=[{"box":(219,99,172,202),"confidence":.88}]
        real={"box":(283,117,58,58)}
        fake={"box":(583,112,53,53)}
        self.assertEqual(_verified_faces([fake,real],people),[real])

    def test_ambiguous_people(self):
        people=[{"box":(10,10,50,100)},{"box":(200,10,50,100)}]
        self.assertIsNone(_choose_person(people))
        self.assertEqual(_choose_person(people,"right"),people[1]["box"])

    def test_crop_must_include_target(self):
        self.assertFalse(_crop_contains((450,0,190,360),(283,117,58,58)))
        self.assertTrue(_crop_contains((220,0,420,360),(283,117,58,58)))

    def test_title_generation_safety_gate(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from core.titles import ensure_safe_titles
        import json
        with TemporaryDirectory() as temp:
            cfg=SimpleNamespace(add_title_overlay=True,resume=False,ollama_host="local",
                ollama_model="test",llm_timeout_seconds=2,llm_num_ctx=2048,
                project_output_dir=lambda:Path(temp))
            logger=SimpleNamespace(info=lambda text:None,warn=lambda text:None)
            for decision in (True,False,"true",None):
                clip=SimpleNamespace(start=0,end=5,text="نص عن الأسرة والتربية",title="Clip 01")
                replies=[json.dumps({"titles":[{"i":0,"title":"كيف تبني بيتا سعيدا"}]}),
                         json.dumps({"checks":[{"i":0,"safe":decision}]})]
                with patch("core.clip_selector._call_ollama",side_effect=replies):
                    ensure_safe_titles([clip],[],cfg,logger)
                self.assertEqual(bool(clip.title),decision is True)
            clip=SimpleNamespace(start=0,end=5,text="نص عن الأسرة",title="Part 1")
            with patch("core.clip_selector._call_ollama",side_effect=RuntimeError("offline")):
                ensure_safe_titles([clip],[],cfg,logger)
            self.assertEqual(clip.title,"")
