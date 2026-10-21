import unittest
from experiments.free_search.owner_robots import PublicRobots
from experiments.free_search.owner_budget import UA


class RobotsTests(unittest.TestCase):
    def check(self, rules, path):
        return PublicRobots(rules.splitlines()).can_fetch(UA, 'https://auto.ria.com'+path)

    def test_actual_bff_and_api_wildcards_blocked(self):
        rules = 'User-agent: *\nDisallow: *bff*\nDisallow: */api/*'
        self.assertFalse(self.check(rules, '/bff/final-page/public/auto/popUp/'))
        self.assertFalse(self.check(rules, '/x/api/search'))
        self.assertTrue(self.check(rules, '/uk/auto_skoda_octavia_39082094.html'))

    def test_wildcard_query_anchor(self):
        rules = 'User-agent: *\nDisallow: /*?secret=*\nDisallow: /private$'
        for path in ['/x?secret=1', '/private']:
            self.assertFalse(self.check(rules, path))
        self.assertTrue(self.check(rules, '/private-page'))

    def test_longest_rule_and_allow_tie(self):
        rules = 'User-agent: *\nDisallow: /\nAllow: /public\nDisallow: /public/private\nAllow: /public/private'
        self.assertTrue(self.check(rules, '/public/private'))
        self.assertFalse(self.check(rules, '/elsewhere'))

    def test_specific_group_and_merged_groups(self):
        rules = 'User-agent: *\nDisallow: /\nUser-agent: AutoDeal-Research\nDisallow: /x\nUser-agent: AutoDeal-Research\nDisallow: /y'
        self.assertTrue(self.check(rules, '/z'))
        self.assertFalse(self.check(rules, '/x'))
        self.assertFalse(self.check(rules, '/y'))

    def test_empty_disallow_and_comments(self):
        self.assertTrue(self.check('User-agent: *\nDisallow: # no restriction', '/'))

    def test_delay_preserved(self):
        robot = PublicRobots(['User-agent: *','Crawl-delay: 12','Request-rate: 1/15'])
        self.assertEqual(robot.crawl_delay(UA), 12)
        self.assertEqual(robot.request_rate(UA).seconds, 15)
