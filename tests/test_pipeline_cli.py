import json

from upgrade_pipeline.cli import COMMANDS, main


def test_help_lists_every_command(capsys):
    assert main(['--help']) == 0
    out = capsys.readouterr().out
    assert all(f'  {name}' in out for name in COMMANDS)


def test_arguments_without_a_command_run_the_pipeline(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr('upgrade_pipeline.cli.run', lambda argv: seen.append(argv) or 0)
    monkeypatch.setitem(COMMANDS, 'run', ('upgrade_pipeline.cli', 'run', 'run'))
    assert main(['--project', 'Orbit']) == 0 and main(['run', '--project', 'Orbit']) == 0
    assert seen == [['--project', 'Orbit'], ['--project', 'Orbit']]


def test_step_commands_dispatch_to_their_modules(monkeypatch):
    seen = {}
    monkeypatch.setattr('upgrade_pipeline.final_output.main', lambda argv: seen.setdefault('final', argv) and 0)
    assert main(['final', 'outputs/x/01-orbit']) == 0 and seen['final'] == ['outputs/x/01-orbit']


def test_schemas_command_writes_and_checks(tmp_path, capsys):
    assert main(['schemas', '--check', '--dir', str(tmp_path)]) == 1
    assert 'stale' in capsys.readouterr().err
    assert main(['schemas', '--dir', str(tmp_path)]) == 0
    assert json.loads((tmp_path / 'final_output.schema.json').read_text())['title'] == 'Upgrade risk final output'
    assert main(['schemas', '--check', '--dir', str(tmp_path)]) == 0
