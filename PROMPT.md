I want you to write me a python script `~/.pi/completion-by-agent.py` that I will use as follows in my zsh shell:

```sh
~/.pi/completion-by-agent.py > ~/.cache/zsh/pi-completion-by-agent.zsh
source ~/.cache/zsh/pi-completion-by-agent.zsh
```

you can look at `~/.cache/zsh/` for examples of other tools that generate completions (the completion scripts themselves)

the script will run `pi --help` and learn the options to pi.
discovering pi subcommands, it will call `pi <command> --help` and learn options to subcommand.
