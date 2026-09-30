import unittest
from unittest.mock import Mock, patch
from config import Config
from committee import TradingCommittee


SIGNAL = {'symbol': 'ETHUSDC', 'rsi': 32.0, 'context_state': 'RANGE_CALME',
          'price': 3000.0, 'bb_position': 'BELOW_LOWER'}


def claude_ok(text):
    resp = Mock()
    resp.json.return_value = {'content': [{'text': text}]}
    return resp


def gpt_ok(text):
    resp = Mock()
    resp.json.return_value = {'choices': [{'message': {'content': text}}]}
    return resp


class TestCommittee(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.committee = TradingCommittee(self.db)
        self._saved = {
            'COMMITTEE_ENABLED': Config.COMMITTEE_ENABLED,
            'COMMITTEE_VOTE_MIN': Config.COMMITTEE_VOTE_MIN,
            'CLAUDE_API_KEY': Config.CLAUDE_API_KEY,
            'OPENAI_API_KEY': Config.OPENAI_API_KEY,
            'GEMINI_API_KEY': Config.GEMINI_API_KEY,
        }
        Config.COMMITTEE_ENABLED = True
        Config.COMMITTEE_VOTE_MIN = 2
        Config.CLAUDE_API_KEY = ''
        Config.OPENAI_API_KEY = ''
        Config.GEMINI_API_KEY = ''

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(Config, k, v)

    def test_no_keys_configured_rejects(self):
        # comite active mais aucune cle -> rejet (pas d'approbation aveugle)
        self.assertFalse(self.committee.evaluate(SIGNAL))

    def test_yes_in_english_counts_as_yes(self):
        Config.CLAUDE_API_KEY = 'k1'
        Config.OPENAI_API_KEY = 'k2'
        with patch('committee.requests.post', side_effect=[
            claude_ok('YES, looks good.'),
            gpt_ok('YES'),
        ]):
            self.assertTrue(self.committee.evaluate(SIGNAL))

    def test_api_error_is_real_abstention(self):
        # 2 modeles configures : un en erreur (abstention, non comptee),
        # l'autre dit YES -> 1 voix / 1 votant, quorum min(2, 1) = 1 -> accepte
        Config.CLAUDE_API_KEY = 'k1'
        Config.OPENAI_API_KEY = 'k2'
        with patch('committee.requests.post', side_effect=[
            ConnectionError('API down'),   # Claude -> None (abstention)
            gpt_ok('YES'),                 # GPT -> True
        ]):
            self.assertTrue(self.committee.evaluate(SIGNAL))

    def test_sole_model_error_rejects(self):
        # un seul modele configure et il echoue -> aucune voix -> rejet
        Config.CLAUDE_API_KEY = 'k1'
        with patch('committee.requests.post', side_effect=ConnectionError('down')):
            self.assertFalse(self.committee.evaluate(SIGNAL))


if __name__ == '__main__':
    unittest.main()
