import json
import tempfile
import unittest
from storage import Store

class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.store=Store(self.temp.name)
        with self.store.change() as d:
            d['users']['1']={'username':'customer'}
        self.store.import_codes(['OLD12345','FREE1234'])
        self.old=self.store.assign(1)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_new_campaign_preserves_history_allows_new_issue(self):
        self.store.new_campaign('Новый год','legacy',99)
        self.assertEqual(self.store.data['campaign_archives']['legacy']['users']['1']['code'],self.old)
        self.assertIsNone(self.store.assign(1))
        self.assertEqual(self.store.import_codes(['NEW12345',self.old]),1)
        with self.store.change() as d:d['settings']['enabled']=True
        self.assertEqual(self.store.assign(1),'NEW12345')
        self.assertEqual(self.store.assign(1),'NEW12345')
        self.store.close();self.store=Store(self.temp.name)
        self.assertEqual(self.store.assign(1),'NEW12345')
        self.assertEqual(self.store.data['campaign_archives']['legacy']['codes'][self.old],'1')

    def test_stale_confirmation_cannot_create_another_campaign(self):
        self.store.new_campaign('Новая','legacy',99)
        with self.assertRaises(ValueError):self.store.new_campaign('Повтор','legacy',99)
        self.assertEqual(len(self.store.data['campaign_archives']),1)

    def test_legacy_migration(self):
        with self.store.change() as d:
            d.pop('campaign');d.pop('campaign_archives')
        self.store.close();self.store=Store(self.temp.name)
        self.assertEqual(self.store.assign(1),self.old)
        self.assertEqual(self.store.data['campaign']['id'],'legacy')

    def test_end_prevents_new_issue(self):
        with self.store.change() as d:d['users']['2']={}
        self.store.finish_campaign('legacy',99)
        self.assertIsNone(self.store.assign(2))
        self.assertIsNone(self.store.assign(1))
        self.assertEqual(self.store.data['users']['1']['code'],self.old)

    def test_jobs_are_archived_not_carried_forward(self):
        with self.store.change() as d:
            d['jobs']['1:coupon']={'user_id':1,'due_at':0}
            d['invitations']['1']={'status':'sent'}
        self.store.new_campaign('Другая','legacy',99)
        self.assertEqual(self.store.data['jobs'],{})
        self.assertEqual(self.store.data['invitations'],{})
        self.assertIn('1:coupon',self.store.data['campaign_archives']['legacy']['jobs'])

if __name__=='__main__':unittest.main()
