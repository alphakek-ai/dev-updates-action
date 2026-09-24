"""Execute the action's actual installer inside a restrictive consumer repository."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


ACTION_PATH = Path(__file__).resolve().parent
STEPS = yaml.safe_load((ACTION_PATH / 'action.yml').read_text())['runs']['steps']
INSTALL = next(step for step in STEPS if step['name'] == 'Install locked delivery dependencies')


@pytest.fixture
def consumer(tmp_path):
    checkout = tmp_path / 'consumer'
    checkout.mkdir()
    # A fixed cutoff keeps the production regression reproducible as packages age.
    (checkout / 'pyproject.toml').write_text(
        '[project]\nname = "consumer"\nversion = "0.0.0"\n'
        'requires-python = ">=99"\n'
        '[tool.uv]\nexclude-newer = "2000-01-01T00:00:00Z"\n'
    )
    (checkout / '.python-version').write_text('99.0\n')
    runner = tmp_path / 'runner'
    runner.mkdir()
    env = {**os.environ, 'ACTION_PATH': str(ACTION_PATH), 'RUNNER_TEMP': str(runner)}
    return checkout, runner, env


def test_locked_install_ignores_consumer_policy_and_python_pin(consumer):
    checkout, runner, env = consumer
    subprocess.run(['bash', '-euo', 'pipefail', '-c', INSTALL['run']],
                   cwd=checkout, env=env, check=True, capture_output=True, text=True)
    python = runner / 'dev-updates-venv/bin/python'
    # Import the real publisher dependencies with the interpreter used for delivery.
    subprocess.run([str(python), '-c', 'import telegramify_markdown, tweepy, requests'],
                   cwd=checkout, env=env, check=True, capture_output=True, text=True)
    # Negative control: the very same lockfile is rejected without isolation.
    rejected = subprocess.run(
        ['uv', 'pip', 'sync', '--dry-run', '--reinstall', '--python', str(python),
         '--require-hashes', str(ACTION_PATH / 'requirements.txt')],
        cwd=checkout, env=env, capture_output=True, text=True,
    )
    assert rejected.returncode != 0
    assert 'exclude-newer' in rejected.stderr


def test_installer_failure_prevents_generation(consumer, tmp_path):
    checkout, runner, env = consumer
    broken_action = tmp_path / 'broken-action'
    broken_action.mkdir()
    # A missing lockfile produces a genuine uv failure without contacting an LLM.
    env['ACTION_PATH'] = str(broken_action)
    marker = runner / 'generation-started'
    relevant = [step for step in STEPS
                if step['name'] in {'Install locked delivery dependencies', 'Generate summaries'}]
    for step in relevant:
        if step['name'] == 'Generate summaries':
            marker.touch()
            break
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', step['run']],
                                cwd=checkout, env=env, capture_output=True, text=True)
        if result.returncode:
            break
    assert not marker.exists()
    assert result.returncode != 0
