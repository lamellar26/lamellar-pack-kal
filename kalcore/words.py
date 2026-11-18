"""Language data for kal. Words to look for in Chinese live in words.json, so the code holds no Chinese text."""
import json, os, re

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'words.json'), encoding='utf-8') as f: W = json.load(f)


def alt(key):
    """The words of one list as a regex alternation (escaped)."""
    return '|'.join(re.escape(w) for w in W[key])
