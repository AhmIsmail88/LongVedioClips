import sys, unittest, json, tempfile, wave, importlib.util
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from config import Config
from core.semantic_candidates import propose_candidates, transcript_chunks, merge_candidates
from core import clip_selector
from core.captions import _group_words, transcript_to_srt
from core.smart_reframe import SpeakerTracker
from core.speaker_focus import MouthFocus, AudioActivity
from models.schemas import CandidateSegment, TranscriptSegment, Word
from utils.audio_split import snap_parts_to_speech
from _harness import QuietLogger

class ContextTests(unittest.TestCase):
    def setUp(self):
        self.segments=[TranscriptSegment(i*5,(i+1)*5,f'idea {i}') for i in range(12)]
        self.config=Config(min_duration=10,max_duration=40)

    def test_only_valid_ids_produce_exact_source_spans(self):
        entries=[{'start_segment':2,'end_segment':5},
                 {'start_segment':-1,'end_segment':2},
                 {'start_segment':True,'end_segment':2},
                 {'start_segment':7,'end_segment':3},
                 {'start_segment':2,'end_segment':100},
                 {'start_segment':2,'end_segment':5}]
        stats={}
        with patch.object(clip_selector,'_call_ollama',return_value=json.dumps({'clips':entries})):
            candidates=propose_candidates(self.segments,self.config,QuietLogger(),stats)
        self.assertEqual(len(candidates),1)
        self.assertEqual((candidates[0].start,candidates[0].end),(10,30))
        self.assertEqual(candidates[0].segment_indices,[2,3,4,5])
        self.assertEqual(candidates[0].context_before,'idea 1')
        self.assertEqual(stats['semantic_invalid_proposals'],4)

    def test_failures_leave_window_candidates_available(self):
        window=CandidateSegment(0,15,'window',[0,1,2])
        with patch.object(clip_selector,'_call_ollama',side_effect=RuntimeError('offline')):
            proposals=propose_candidates(self.segments,self.config,QuietLogger())
        self.assertEqual(merge_candidates([window],proposals,self.config),[window])

    def test_every_segment_occurs_in_a_bounded_chunk(self):
        segments=[TranscriptSegment(i,i+1,'x'*2000) for i in range(21)]
        seen=set()
        for start,end,text in transcript_chunks(segments):
            seen.update(range(start,end))
            self.assertLessEqual(len(text),4500)
        self.assertEqual(seen,set(range(21)))

    def test_successful_proposals_resume_but_changed_transcript_recomputes(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/"source.mp4"
            source.write_bytes(b"source")
            config=Config(video_path=str(source),output_dir=folder,min_duration=10,max_duration=40)
            response=json.dumps({"clips":[{"start_segment":2,"end_segment":5}]})
            with patch.object(clip_selector,"_call_ollama",return_value=response) as call:
                first=propose_candidates(self.segments,config,QuietLogger())
                second=propose_candidates(self.segments,config,QuietLogger())
                self.assertEqual(call.call_count,1)
                self.assertEqual(first,second)
                self.segments[2].text="changed idea"
                propose_candidates(self.segments,config,QuietLogger())
                self.assertEqual(call.call_count,2)

    def test_serialization_remains_backwards_compatible(self):
        old=CandidateSegment.from_dict({'start':0,'end':20,'text':'old'})
        self.assertEqual(old.source,'window')
        current=CandidateSegment(0,20,'idea',source='semantic',context_before='setup')
        self.assertEqual(CandidateSegment.from_dict(current.to_dict()),current)

    def test_ranker_knows_context_is_excluded_from_clip(self):
        candidate=CandidateSegment(0,20,'answer',context_before='question')
        prompt=clip_selector._build_user_prompt([candidate])
        self.assertIn('context_before (not included): question',prompt)
        self.assertIn('text: answer',prompt)

    def test_hybrid_analysis_merges_proposals_before_ranking(self):
        import app
        from core import semantic_candidates
        from _harness import stub_score_candidates
        with tempfile.TemporaryDirectory() as folder:
            config=Config(video_path=str(Path(folder)/'source.mp4'), output_dir=folder,
                          num_clips=2,min_duration=10,max_duration=20,resume=False,
                          candidate_strategy='hybrid')
            proposal=CandidateSegment(0,15,' '.join(s.text for s in self.segments[:3]),
                                      [0,1,2],source='semantic')
            with patch.object(app,'check_ffmpeg_installed'), \
                 patch.object(app,'probe_duration',return_value=60), \
                 patch.object(app.transcriber,'transcribe',return_value=self.segments), \
                 patch.object(semantic_candidates,'propose_candidates',return_value=[proposal]) as propose, \
                 patch.object(app.clip_selector,'score_candidates',side_effect=stub_score_candidates):
                clips,_,_=app.analyze(config,QuietLogger())
            self.assertTrue(clips)
            propose.assert_called_once()
            saved=json.loads(config.candidates_path().read_text(encoding='utf-8'))
            self.assertTrue(any(c['source']=='semantic' for c in saved))

    def test_batch_limit_one_is_respected(self):
        self.assertEqual(clip_selector.llm_batch_size_for(Config(llm_batch_size=1)),1)
        self.assertEqual(clip_selector.llm_batch_size_for(Config(llm_batch_size=1),
                         [CandidateSegment(0,60,'x'*1200)]),1)

class BoundariesAndCaptionsTests(unittest.TestCase):
    def test_shared_audio_boundary_moves_off_a_word_without_gaps(self):
        segments=[TranscriptSegment(9,11,'word',[Word(9,11,'word')])]
        parts=snap_parts_to_speech([(0,10),(10,20)],segments)
        self.assertEqual(parts[0][1],parts[1][0])
        self.assertIn(parts[0][1],(9,11))
        self.assertEqual(parts[0][0],0)
        self.assertEqual(parts[-1][1],20)

    def test_audio_boundary_uses_pause_without_changing_part_count(self):
        segments=[TranscriptSegment(0,20,'words',[Word(8,9,'first'),Word(11,12,'second')])]
        parts=snap_parts_to_speech([(0,9.5),(9.5,20)],segments)
        self.assertEqual(parts,[(0,10),(10,20)])

    def test_captions_do_not_span_silence_or_merge_sentence_ends(self):
        words=[Word(0,1,'Hello.'),Word(1,2,'Next'),Word(4,5,'later')]
        self.assertEqual(_group_words(words,5),[(0,1,'Hello.'),(1,2,'Next'),(4,5,'later')])

    def test_srt_timestamps_roll_over_and_preserve_arabic(self):
        text=transcript_to_srt([TranscriptSegment(59.9999,61,'مرحبا')])
        self.assertIn('00:01:00,000 --> 00:01:01,000',text)
        self.assertIn('مرحبا',text)

class FaceIdentityTests(unittest.TestCase):
    def test_two_nearby_faces_never_share_one_tracking_id(self):
        tracker=SpeakerTracker()
        candidates=[{'box':[100,0,80,80],'score':6400},
                    {'box':[130,0,80,80],'score':6400}]
        tracker.get_target(candidates,0,1920)
        self.assertEqual(len({f['id'] for f in tracker.known_faces}),2)
        tracker.get_target(candidates,2,1920)
        self.assertEqual(len({f['id'] for f in tracker.known_faces}),2)

    def test_expired_face_tracks_are_removed(self):
        tracker=SpeakerTracker(forget_after_frames=3)
        tracker.get_target([{'box':[10,0,80,80],'score':6400}],0,1920)
        tracker.get_target([],10,1920)
        self.assertEqual(tracker.known_faces,[])

HAS_CV=bool(importlib.util.find_spec('cv2'))
@unittest.skipUnless(HAS_CV,'opencv required for motion tests')
class MotionFocusTests(unittest.TestCase):
    boxes=[(0,0,64,64),(80,0,64,64)]
    def frame(self,side,step,whole=False):
        import numpy as np
        frame=np.zeros((64,144,3),dtype='uint8')
        x=0 if side=='left' else 80
        if whole:
            frame[:,x:x+64]=255 if step%2 else 0
        else:
            frame[40:59,x+12:x+52]=255 if step%2 else 0
        return frame

    def test_sustained_mouth_motion_with_audio_selects_and_switches(self):
        focus=MouthFocus()
        for i in range(10):
            side=focus.update(self.frame('left',i),self.boxes,i/10,.1)
        self.assertEqual(side,'left')
        for i in range(10,26):
            side=focus.update(self.frame('right',i),self.boxes,i/10,.1)
        self.assertEqual(side,'right')

    def test_no_audio_and_global_motion_do_not_select_a_speaker(self):
        focus=MouthFocus()
        for i in range(10):
            self.assertIsNone(focus.update(self.frame('left',i),self.boxes,i/10,0))
        focus=MouthFocus()
        for i in range(10):
            self.assertIsNone(focus.update(self.frame('left',i,True),self.boxes,i/10,.1))

    def test_missing_pair_clears_focus(self):
        focus=MouthFocus()
        for i in range(10):
            focus.update(self.frame('left',i),self.boxes,i/10,.1)
        self.assertIsNone(focus.update(self.frame('left',0),[],1,.1))

    def test_pcm_activity_detects_sound_and_silence(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'activity.wav'
            data=np.concatenate([np.zeros(16000,dtype='<i2'),np.ones(16000,dtype='<i2')*10000])
            with wave.open(str(path),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
                audio.writeframes(data.tobytes())
            activity=AudioActivity(path)
            self.assertEqual(activity.rms_at(.5),0)
            self.assertGreater(activity.rms_at(1.5),.2)

if __name__=='__main__':
    unittest.main()
