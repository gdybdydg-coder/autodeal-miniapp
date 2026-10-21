"""Robots path rules including '*' and '$'; no network or access overrides.

urllib.robotparser treats wildcards literally. Preserve its delay parsing, but
apply path rules ourselves so '*bff*' cannot accidentally allow a BFF route.
Only the explicit ASCII public URLs accepted by owner_budget reach this gate.
"""
import re
import urllib.robotparser
from urllib.parse import urlsplit


class PublicRobots:
    def __init__(self, lines):
        self.delays = urllib.robotparser.RobotFileParser()
        self.delays.parse(lines)
        self.groups = []
        agents, rules, directives = [], [], False
        for raw in lines:
            line = raw.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = (s.strip() for s in line.split(':', 1))
            key = key.lower()
            if key == 'user-agent':
                if directives:
                    self.groups.append((agents, rules))
                    agents, rules, directives = [], [], False
                agents.append(value.lower())
            elif agents:
                directives = True
                if key in ('allow', 'disallow') and value:
                    end = value.endswith('$')
                    raw_pattern = value[:-1] if end else value
                    pattern = '^' + '.*'.join(re.escape(p) for p in raw_pattern.split('*'))
                    if end:
                        pattern += '$'
                    rules.append((len(raw_pattern.replace('*', '')), key == 'allow', re.compile(pattern)))
        if agents:
            self.groups.append((agents, rules))

    def can_fetch(self, agent, url):
        product = agent.split('/', 1)[0].lower()
        selected, specificity = [], -1
        for agents, rules in self.groups:
            matches = [0 if a == '*' else len(a) for a in agents if a == '*' or a in product]
            if not matches:
                continue
            level = max(matches)
            if level > specificity:
                selected, specificity = list(rules), level
            elif level == specificity:
                selected.extend(rules)
        parsed = urlsplit(url)
        path = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
        matched = [(length, allow) for length, allow, pattern in selected if pattern.search(path)]
        return not matched or max(matched)[1]  # longest match, Allow wins a tie

    def crawl_delay(self, agent):
        return self.delays.crawl_delay(agent)

    def request_rate(self, agent):
        return self.delays.request_rate(agent)
