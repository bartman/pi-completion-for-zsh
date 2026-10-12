# Zsh completion for Pi

The [pi](https://github.com/earendil-works/pi/) harness is minimal.  It does not come with shell
completion.

But of course LLMs can build that for you.  Here are 3 attempts at python scripts that generate
zsh tab completions from `pi --help` output. Each model was given the same [prompt](PROMPT.md).

- [completion-by-claude.py](completion-by-claude.py) - `claude-code`, running `claude-opus-5-5` generating up 85.8k tokens.
- [completion-by-grok.py](completion-by-grok.py) - `grok-build`, running `grok-4.7` generating up 114k tokens.
- [completion-by-muse.py](completion-by-muse.py) - `pi`, running `muse-spark-1.3-contributor` generating up 78.8k tokens;

The python scripts, when ran along side `pi v1.1.0` will generate the following zsh completion scripts:

- [completion-by-claude-v1.1.0.zsh](completion-by-claude-v1.1.0.zsh)
- [completion-by-grok-v1.1.0.zsh](completion-by-grok-v1.1.0.zsh)
- [completion-by-muse-v1.1.0.zsh](completion-by-muse-v1.1.0.zsh)

## install (static)

You can take the generated .zsh files and source it into your `~/.zshrc`.
Save one of the above `.zsh` files as `~/.cache/zsh/pi-completion.zsh`.
Put this in your `~/.zshrc` ...

```zsh
if [ -n "$PS1" ]; then
    local _my_pi_completion_file=~/.cache/zsh/pi-completion.zsh
    source $_my_pi_completion_file
fi
```

If version of pi changes, you will not get completions for new features.


## install (dynamic)

Alternatively, you can use the python files to regenerate the completion script when the `pi`
executable changes (new version == new options).

Copy the `.py` scripts into `~/.pi/` directory.
Put this in your `~/.zshrc` ...

```zsh
if [ -n "$PS1" ]; then
    _my_pi_completion() {
        local _real_pi="$(whence -p pi)"
        if [ -z "$_real_pi" ] ; then
            echo "ERROR: could not find pi executable"
            return 1
        fi
        if [ -f "$_real_pi" ] ; then
            local _my_pi_completion_zsh=~/.pi/completion-by-muse.py         # pick the one you want to use
            local _my_pi_completion_dir=~/.cache/zsh
            local _my_pi_completion_file=$_my_pi_completion_dir/pi-completion.zsh

            if [ -f $_my_pi_completion_zsh ] ; then
                if ! [ -f $_my_pi_completion_file ] || [ $(realpath $_real_pi) -nt $_my_pi_completion_file ] ; then

                    mkdir -p $_my_pi_completion_dir

                    $_my_pi_completion_zsh > $_my_pi_completion_file
                fi
            fi

            if [ -s $_my_pi_completion_file ] ; then
                source $_my_pi_completion_file
            fi
        fi
    }
    _my_pi_completion
fi
```
