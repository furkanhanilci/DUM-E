# Telegram control bridge — setup

Telegram is a convenience surface, not authority. Every message goes through the
same command gateway the CLI uses, and nothing reaches a shell.

## What you do

**1. Create a bot.** Message [@BotFather](https://t.me/BotFather) on Telegram,
send `/newbot`, and follow it. It returns a token that looks like
`123456789:AAF...`. That token *is* the bot — treat it as a password.

**2. Find your numeric user id.** Message [@userinfobot](https://t.me/userinfobot);
it replies with your id. It is a number, not your @username — the bridge
authenticates on the id because a username can be changed and reused.

**3. Write the configuration**, with the mode set before the content:

```bash
install -m 600 /dev/null ~/.dume/secrets/telegram.json
cat > ~/.dume/secrets/telegram.json <<'JSON'
{
  "token": "<the token BotFather gave you>",
  "allowed": {
    "<your numeric id>": { "name": "Furkan", "max_class": "DANGEROUS_ACTION" }
  },
  "poll_timeout": 25
}
JSON
```

`~/.dume/secrets/` is on ext4 and the mode is enforced there. Do not put this on
`/media/otonom/DATADRIVE1` — that mount is NTFS and silently discards `chmod`.

**4. Check it, then run it.**

```bash
python3 -m dume.cli telegram --check   # names the bot and who may command it
python3 -m dume.cli telegram           # polls until Ctrl-C
```

## `max_class`

| Value | Can do |
|---|---|
| `READ` | status, show, history, findings, runtimes, next, evidence |
| `CONTROL` | …and pause, resume, retry, reserve/release/disable/enable a runtime |
| `HUMAN_DECISION` | …and decide, block |
| `DANGEROUS_ACTION` | …and kill, bind_workspace — each needing a second confirming message |

Give a second person `READ` if they should watch without steering. There is no
value that grants a shell, because there is no shell.

## What it refuses, and why

| Attempt | What happens |
|---|---|
| `rm -rf /`, `sudo …`, `$(…)`, `curl … \| sh` | refused as shell-shaped content |
| "ignore all previous instructions and …" | refused as instruction-shaped content — it is data |
| `accept WP-001` | there is no `accept` command. Acceptance needs independent verification evidence bound to a candidate; a message is not that |
| a **forwarded** message | refused, whatever it says. Someone can be persuaded to forward anything |
| a message from anyone not in `allowed` | refused. Adding the bot to a group does not enfranchise the group |
| more than 30 commands a minute | rate-limited |
| `kill` | asks for `confirm <nonce>`, from you, within 120 seconds |

Every one of those — accepted and refused alike — is appended to
`evidence/command_audit.jsonl`. The refusals are the interesting half.

## Commands

`python3 -m dume.cli command --vocabulary` prints the whole set. The same
vocabulary works from the CLI, from Telegram, and from Buzz — one gateway, so a
new surface cannot widen it.

Useful ones:

```
status                     where every package stands
next                       what could start now, and what blocks the rest
show WP-005                one package in detail
runtimes                   what is available, reserved, qualified
reserve claude-fable-5     keep a scarce quota for architecture-critical work
pause                      stop starting new work; running work finishes
block WP-012 relay flaky   record a human block with a reason
kill                       stop the model servers (asks first)
```

## The token

It never enters the repository, a work-package packet, an evidence file or a log
line. It is redacted from every error this bridge raises — including the ones
the HTTP library builds, because the token is in the URL.
