import unittest
from unittest.mock import Mock, patch
from config import Config
from committee import TradingCommittee


SIGNAL = {'symbol': 'ETHUSDC', 'rsi': 32.0, 'context_state': 'RANGE_CALME',
          'price': 3000.0, 'bb_position': 'BELOW_LOWER'}


def ok_response(text):
    resp = Mock()
    resp.json.return_value = {'content': [{'text': text}]}
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
            ok_response('YES, looks good.'),                    # Claude
            Mock(json=lambda: {'choices': [{'message': {'content': 'YES'}}]}),  # GPT
        ]):
            self.assertTrue(self.committee.evaluate(SIGNAL))

    def test_api_error_is_real_abstention(self):
        # 2 modeles : un en erreur (abstention), un qui dit YES -> 1/1 >= quorum 1
        Config.CLAUDE_API_KEY = 'k1'
        Config.OPENAI_API_KEY = 'k2'
        def post_side_effect(*a, **kw):
            raise ConnectionError('API down')
        with patch('committee.requests.post', side_effect=post_side_effect) as p:
            p.return_value = ok_response('YES')
            # Claude: erreur -> abstention ; GPT: reponse ok
            # (side_effect precede return_value : on patche proprement)
        with patch('committee.requests.post') as p:
            p.side_effect = [ConnectionError('API down'), ok_response('YES')]
            # Claude leve -> None (abstention) ; GPT dit YES
            # mais GPT lit resp.json()['choices'][0]['message']['content']
            p.side_effect = [
                ConnectionError('API down'),
                Mock(json=lambda: {'choices': [{'message': {'content': 'YES'}}]}),
            ]
            self.assertTrue(self.committee.evaluate(SIGNAL))

    def test_sole_model_error_rejects(self):
        # un seul modele configure et il echoue -> aucune voix -> rejet
        Config.CLAUDE_API_KEY = 'k1'
        with patch('committee.requests.post', side_effect=ConnectionError('down')):
            self.assertFalse(self.committee.evaluate(SIGNAL))


if __name__ == '__main__':
    unittest.main()
