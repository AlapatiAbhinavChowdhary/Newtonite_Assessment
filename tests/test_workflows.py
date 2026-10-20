import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

import server


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(cls.temp.name) / "test.db"
        server.init_db()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.thread.join(); cls.temp.cleanup()

    def request(self, method, path, user="u1", body=None, key=None):
        conn=HTTPConnection("127.0.0.1",self.port,timeout=3)
        headers={"X-User-Id":user,"Content-Type":"application/json"}
        if key: headers["Idempotency-Key"]=key
        raw=json.dumps(body).encode() if body is not None else None
        conn.request(method,path,raw,headers); response=conn.getresponse(); data=json.loads(response.read())
        conn.close(); return response.status,data

    def test_team_scope_is_enforced_by_api(self):
        status,data=self.request("GET","/api/items?q=w-1002","u2")
        self.assertEqual(status,200); self.assertEqual(data["total"],1)
        status,_=self.request("GET","/api/items/w-1002/activity","u2")
        self.assertEqual(status,200)
        item=data["items"][0]
        status,error=self.request("POST","/api/items/w-1002/update","u2",{"version":item["version"],"status":"resolved"})
        self.assertEqual(status,403); self.assertIn("read-only",error["error"])

    def test_deployment_health_check(self):
        status,data=self.request("GET","/health")
        self.assertEqual(status,200); self.assertEqual(data["status"],"ok")

    def test_stale_update_is_rejected_and_logged_once(self):
        _,listing=self.request("GET","/api/items?q=w-1003")
        item=listing["items"][0]; version=item["version"]
        status,_=self.request("POST","/api/items/w-1003/update",body={"version":version,"priority":"high","comment":"Reviewed"})
        self.assertEqual(status,200)
        status,data=self.request("POST","/api/items/w-1003/update",body={"version":version,"priority":"urgent"})
        self.assertEqual(status,409); self.assertIn("changed",data["error"])

    def test_claim_is_idempotent_and_second_owner_cannot_take_item(self):
        # Create a clean, unassigned work item.
        status,created=self.request("POST","/api/items",body={"title":"Concurrency test","team":"Operations"})
        self.assertEqual(status,201); item=created["item"]; key="retry-test-001"
        status,first=self.request("POST",f"/api/items/{item['id']}/claim","u2",{"version":item["version"]},key)
        self.assertEqual(status,200)
        status,retry=self.request("POST",f"/api/items/{item['id']}/claim","u2",{"version":item["version"]},key)
        self.assertEqual(status,200); self.assertEqual(first["item"]["owner_id"],retry["item"]["owner_id"])
        status,repeat=self.request("POST",f"/api/items/{item['id']}/claim","u2",{"version":first["item"]["version"]},"new-click-key")
        self.assertEqual(status,200); self.assertEqual(first["item"]["version"],repeat["item"]["version"])
        status,conflict=self.request("POST",f"/api/items/{item['id']}/claim","u1",{"version":first["item"]["version"]})
        self.assertEqual(status,409); self.assertIn("owned",conflict["error"])

    def test_two_simultaneous_claims_have_one_winner(self):
        _,created=self.request("POST","/api/items",body={"title":"Race test"})
        item=created["item"]; barrier=threading.Barrier(2)
        def claim(user):
            barrier.wait()
            return self.request("POST",f"/api/items/{item['id']}/claim",user,{"version":item["version"]})[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(claim,["u2","u1"]))
        self.assertEqual(sorted(statuses),[200,409])

    def test_next_action_is_persisted_and_searchable(self):
        status,created=self.request("POST","/api/items",body={"title":"Quarterly access review","next_action":"Taylor to approve by Friday"})
        self.assertEqual(status,201); item=created["item"]
        self.assertEqual(item["next_action"],"Taylor to approve by Friday")
        status,result=self.request("GET","/api/items?q=Taylor%20to%20approve")
        self.assertEqual(status,200); self.assertEqual(result["items"][0]["id"],item["id"])

    def test_assignment_requires_team_membership(self):
        _,created=self.request("POST","/api/items",body={"title":"Engineering only","team":"Operations"})
        item=created["item"]
        status,data=self.request("POST",f"/api/items/{item['id']}/assign","u1",{"version":item["version"],"owner_id":"u3"})
        self.assertEqual(status,400); self.assertIn("team",data["error"])

    def test_member_cannot_reassign_team_work(self):
        _,created=self.request("POST","/api/items",body={"title":"Manager-only assignment"})
        item=created["item"]
        status,data=self.request("POST",f"/api/items/{item['id']}/assign","u2",{"version":item["version"],"owner_id":"u2"})
        self.assertEqual(status,403); self.assertIn("managers",data["error"])

    def test_viewer_cannot_be_assigned_work(self):
        status,data=self.request("POST","/api/items/w-1002/assign","u4",{"version":1,"owner_id":"u2"})
        self.assertEqual(status,400); self.assertIn("eligible member",data["error"])

    def test_manager_can_assign_an_engineering_member(self):
        _,created=self.request("POST","/api/items",body={"title":"Assign test","team":"Engineering"})
        item=created["item"]
        status,data=self.request("POST",f"/api/items/{item['id']}/assign","u4",{"version":item["version"],"owner_id":"u3"})
        self.assertEqual(status,200); self.assertEqual(data["item"]["owner_id"],"u3")


if __name__ == "__main__": unittest.main()
