import unittest,tempfile,json,importlib.util
from pathlib import Path
from launch_monitor import load_nodes
class ConfigTests(unittest.TestCase):
 def test_four_nodes_isolated(self):
  n=load_nodes(Path(__file__).parents[1]/'nodes.example.json');self.assertEqual(len(n),4);self.assertEqual(len({x['referee_port'] for x in n}),4)
 def test_duplicate_port_rejected(self):
  n={'nodes':[{'id':'a','host':'1','referee_port':8611},{'id':'b','host':'2','referee_port':8611}]}
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'nodes.json';p.write_text(json.dumps(n))
   with self.assertRaises(ValueError):load_nodes(p)
 def test_unsafe_id_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'nodes.json';p.write_text(json.dumps({'nodes':[{'id':'../escape','host':'1','referee_port':8611}]}))
   with self.assertRaises(ValueError):load_nodes(p)
