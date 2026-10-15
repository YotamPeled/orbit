# Changelog

Every orbit-core release, newest first. A release is a git tag; an instance adopts one
by bumping `orbit-core.lock`, running `make core-sync`, and applying (`orbit platform
apply`) — `make verify` stage6 then proves the live platform is the committed one.

> Until 2026-10-04 release history lived inside docs/ROADMAP.md, mixed with the plans
> that produced it. The plans are superseded by docs/V1.md (the finish line and the only
> progress tracker); the history moved here, one line per tag, from the tags and their
> release commits.

## Unreleased

## v1.0.5 (2026-10-15) — the first public release

The code of v1.0.4, published as a public repository with a history that begins here.

- The instance strings of the instance Orbit was built and tested on (names, addresses,
  domains, ids) are gone from code, tests and documents; the documents' evidence reads
  `example.com`, `example-org` and 203.0.113.x where it named them.
- `scripts/personal_strings.py`, run by the hooks in `.githooks` before every commit and
  every push and by `scripts/test_personal_strings.py` over the whole tree, refuses a
  personal string (held only as salted hashes), a personal mail address, a home-directory
  path, a public IPv4 address, a credential by its shape, another author and a co-author
  trailer. `scripts/local-tests.sh` turns the hooks on.
- The agent scenarios read the instance's domains and GitHub org from its
  `instance/config.yaml` (`ORBIT_SCENARIO_DOMAIN`, `ORBIT_SCENARIO_DEV_DOMAIN`,
  `ORBIT_SCENARIO_ORG`) instead of carrying them.
- `scripts/test_retired_apps_complete.py` reads the platform Applications deleted before
  this history began from a list cut from that history, and every later deletion from git.
- `template/orbit-core.lock` pins this repository at v1.0.5.

Instance step: point `orbit-core.lock` at this repository (`url:`), then the usual bump.

## v1.0.4 (2026-10-13) — a removed project's hand edits go with it

Found by the closing re-test of the v1.0.0 findings on v1.0.3.

- `orbit_deploy_status` showed, as a project's `recent_hand_edits`, the hand edits undone
  for a removed project of the same name an hour before this one existed. The reconcile
  loop forgets a pair the catalog no longer carries.

No instance step beyond the usual bump.

## v1.0.3 (2026-10-13) — a refused ship names the image

Found by the onboarding scenario run on v1.0.2: the agent mistyped one component's image
in the catalog, its ship of three components failed at `registry` with "manifest not
found" and nothing else, and it looked for the fault in orbitd's registry access.

- A ship that fails at the `registry` step names the image, the tag and the component it
  asked for. `skills/orbit-ship` says what to read there.

No instance step beyond the usual bump.

## v1.0.2 (2026-10-12) — a project's deploy history is its own

Found by the agent scenarios run against v1.0.1's code on the test box, where a second
project had just been shipped some forty times.

- `orbit_ledger_get` and `orbit_deploy_history` listed every commit of the tags file for
  every pair the file still carried, so one project's ships filled another's history: the
  newest thirty entries of the first project's dev were the second project's deploys, and
  an agent reading the last ten for the tag to roll back to found the same tag ten times.
  Both now list only the commits that changed what was asked about (the pair, or the one
  component named). `skills/orbit-deploy-review` says so.

No instance step beyond the usual bump.

## v1.0.1 (2026-10-12) — what a first user of v1.0.0 ran into

A second project was onboarded onto the v1.0.0 test box and used the way a new user would
(ships good and bad, monitors, a crash loop, an out-of-memory kill, an accidental wipe and
its restore, hand edits, a reboot, removal and re-creation). Forty findings came out of
it; this release is their fixes, re-tested on the box through three release candidates
(what those showed is fixed here too). docs/V1.md §13 lists each finding with its proof.

**An instance does one thing by hand:** `Makefile` is not vendored. Replace its `check`
target with the template's (`template/Makefile`): the old one fails on any change that is
not committed yet.

What could stop a deploy or mislead about one:

- A catalog change the commit gate would refuse is refused **before it is pushed**:
  `orbit_catalog_set` runs the gate's own checks on the catalog with the entry spliced
  in, in its own process, before the commit script runs (`catalog-commit.sh` does the
  same for a caller at the desk with `ORBIT_CATALOG_PRECHECK=1`, and has a `check` verb).
  It used to land on main and be refused there, and a refused main blocks every deploy
  of every project.
- On a main the gate has refused, passing the last accepted entry back to
  `orbit_catalog_set` commits it over the refused one. It used to answer `unchanged` and
  write nothing, which is what the skill said to do to get out.
- `orbit_deploy_ship` and `orbit_deploy_rollback` answer `main_refused` before writing
  anything while main is refused, instead of landing a bump and ending
  `LANDED NOT DEPLOYED apply`.
- `orbit_catalog_set(dry_run=true)` checks the entry (generator and gate) and says what
  would refuse it. It used to answer `dry_run` for any entry.
- The first prod ship of a project on its default hostname no longer fails with "no
  public hostname in the catalog": the door is asked at the hostname the generator gave
  it. A prod web component with no public door is asked inside the cluster.
- A ship stops waiting when the new version crash-loops (third crash) or cannot pull its
  image, and says so in the kubelet's words; a rollout timeout names what the new pod was
  doing. `orbit_deploy_status` shows `rollout: complete | progressing | stuck` with the
  pod that holds it up, and `drift` is true while any pod still runs another tag.
- New tool `orbit_job_cancel` (operator): stop a ship, rollback or restart that is only
  waiting, and free its project/environment.
- A rollback (or restart, or re-ship of the running tag) that starts no new pod no longer
  ends `LANDED NOT DEPLOYED log` when the pods that kept serving wrote nothing in the
  last 15 minutes. They were not restarted and owe no new line; the step says they were
  quiet. Found on the release candidate: two good rollbacks of a service without traffic
  were reported as failed.

Hostnames:

- A project cannot take a platform hostname (`grafana`, `orbit`, `mcp`, `intake` under the
  primary domain).
- A hostname outside the instance's domains under `hostnames` is refused; a domain of the
  project's own is a `customDomains` entry.
- The chart's Certificate and Middleware carry the pair's labels, so they can be pruned
  once the render no longer holds them: `orbit_deploy_status` lists such an object under
  `reconcile.would_prune` and `orbit_deploy_reconcile` deletes it. Without a label the
  Certificate of a removed custom domain was withheld from every prune and retried its
  order for ever.

Monitors (reference: docs/MONITORS.md, new):

- `orbit_project_remove` removes the project's monitors: their files in the removal
  commit, their rules and probes in a new `monitors` step before anything they watch is
  deleted.
- A log monitor is accepted wherever the instance's Loki application carries the ruler
  wiring, which the template's does for every instance with the `logs` feature on. A
  stock instance refused every log monitor.
- `make verify` counts a probe monitor's target as declared.
- New tool `orbit_monitor_unsilence` (operator). `orbit_monitor_status` returns each
  monitor's whole `definition` and its `silences` in force; `orbit_monitor_list` returns
  `notify`.
- `orbit_monitor_status` reads a log monitor's state from the Loki ruler (it read
  `inactive` while the alert fired: the rule is not among Prometheus's).
- The last log monitor's rule leaves the ruler: with no log monitor declared the shared
  ConfigMap is rendered with no rule in it, and a monitor delete and a project removal
  apply the monitors that remain before they prune.

Databases:

- `orbit_db_status` reads the recovery window from the barman ObjectStore (it was null for
  every database), says `archiving` only where something is archived, shows the last
  backup file made through the tools, and has `state: ready | not ready | hibernated`
  from the instances that are up.
- `orbit_db_restore(to=...)` refuses a moment in the future or before the first base
  backup at once, instead of failing after ten minutes.
- A removed project's backup note goes with it (the files stay and the removal names
  them): a project made again under the same name starts with no backup of its own.

Other tool answers:

- New tool `orbit_catalog_history` (viewer): the commits that changed a project's catalog
  entry, when, by whom, which fields.
- `orbit_size_recommend` counts out-of-memory kills restart by restart, minute by minute.
- `orbit secrets verify` reads a sealed file written with `stringData` (the form the
  onboarding documents' own command seals).
- `orbit_deploy_wait` waits for the checkout to reach a commit that adds the project
  instead of answering `not_found`.
- `orbit_workload_restart` on a scheduled job is `not_restartable`, with the reason.
- `orbit_traces_find` leaves health checks and scrapes out unless `probes` is true.
- `orbit_metrics_query` refuses a range of more than 11 000 points naming the step that
  fits.
- The reconcile tick names every hand edit it undoes: also a field of orbit's changed by
  hand (a scale) and an object deleted by hand. `remove-project.sh` deletes the pair's
  inventory with its namespace, and an inventory older than its namespace is not read as
  evidence of a deletion (a project made again under a removed project's name).
- `orbit_features_list`: a feature's memory is its own workloads', not its namespaces'.
- `orbit_catalog_schema` returns the shape of a catalog entry beside the chart's schema.
- `orbit tools` names each tool's arguments; `--json` carries the input schema.
- The generator's hostname-collision refusal names both owners, and a bad schedule is
  shown as written.
- `make check` compares the generated tree with what the catalog generates, committed or
  not; `make verify` no longer counts a pod that is gone or not started as a failed log
  read.

Documents: docs/MONITORS.md; docs/SKILLS.md brought up to date; the references that sent
a reader to documents removed from `docs/` now say they are removed records; the catalog
template and the onboarding skill say that a web component's `/metrics` and health checks
are public on its hostname; the ship skill's `orbit platform apply` carries its directory.

## v1.0.0 (2026-10-11) — Orbit V1

- The code of v0.34.34. What V1 is, and the proof of each part of it, is docs/V1.md: a single
  node rebuilt from bare by an agent, the operating scenarios (28 of 28) and the
  observability drivers on it, sizing advice, and the releases v0.34.0 to v0.34.34 that the work
  on the way there produced, each with its entry below.
- `docs/PRODUCT.md`: the sizing tables are the ones measured on the rebuilt box.

## v0.34.34 (2026-10-11) — a quota is lowered again after a restart

- v0.34.30 lowers a quota only once the pair's Deployments are "as rendered and done
  rolling". "As rendered" compared the whole object, and a Deployment restarted through
  orbitd keeps the restart's stamp for good, so in a namespace where anything had ever been
  restarted the quota was never lowered again (it stayed at the larger, older value: safe,
  and not what the catalog says). It now compares what the pods ask for, which is what a
  quota counts.

## v0.34.33 (2026-10-09) — no change to what runs

- The same code as v0.34.32. It exists so that the core-upgrade scenario (S15) of the counted
  run has a newer release to upgrade the rebuilt box to.

## v0.34.32 (2026-10-09) — sizing advice tells a crash loop from a few old restarts

- `orbit_size_recommend` refused to suggest less for any component that had restarted three
  times in the window, and said "fix the crash before sizing". A component that was ready
  for nearly all of its window was refused for a week over restarts of builds long since
  replaced. It now also reads the share of the window the component was ready: the refusal
  stands when that is under 90% or not known, and its sentence gives the share; at 90% or
  more the advice is given and says so.

## v0.34.31 (2026-10-09) — no change to what runs

- The same code as v0.34.30. It exists so that the core-upgrade scenario (S15) of the counted
  run has a newer release to upgrade the rebuilt box to.

## v0.34.30 (2026-10-09) — giving a component less memory no longer jams its namespace

- A project's quota is derived from its catalog, so a component given less memory lowered
  the quota in the same commit, and the quota was applied before the workloads: the
  rollout's new pod was refused while the old, larger pod still ran, the old pod was
  therefore never replaced, and the namespace refused every pod from then on, later ships
  included. orbitd now raises a quota at once and lowers it only when the pair's
  Deployments are what was asked for and done rolling (the reconcile pass after the
  rollout, three minutes at most).
- **Upgrade note:** a namespace already jammed this way (`exceeded quota` in its events, a
  Deployment with fewer updated than wanted replicas) is not freed by the upgrade.
- The scenario suite: S22's setup asks for 960Mi (the API server keeps 1024Mi as `1Gi`, and
  the setup waited for the other spelling); its teardown waits for the pods, not only the
  declared sizes; S06e's fault variant has a name that says nothing.

## v0.34.29 (2026-10-09) — no change to what runs

- The same code as v0.34.28. It exists so that the core-upgrade scenario (S15) of the counted
  run has a newer release to upgrade the rebuilt box to.

## v0.34.28 (2026-10-09) — an alert's description reaches the agent

- `orbit_alerts_active` returns each alert's `description` beside its `summary`. It was
  dropped whenever a rule had a summary, which is every rule the platform ships, so the
  sentence that says what to do next (for a workload at its memory limit: "ask
  orbit_size_recommend") never reached an agent. The two alert skills say to read it.
- `db-backup.sh` never takes the name of a backup that exists (names are stamped to the
  second; the backup a restore takes first could overwrite the one being restored).

## v0.34.27 (2026-10-09) — a project made under a removed name archives; a failed restore run cleans up after itself

- A removed project's database archive stays in the bucket, and a project made again under
  the name was pointed at it: barman refuses a path holding another database's files, so the
  new database ran and archived nothing. `orbit_project_remove` now writes the name and its
  archive generation in `catalog/retired.yaml`, and the generator gives a project of that
  name the next generation (`database.archiveGeneration`, a path of its own). A generation
  already used is refused. `remove-project.sh` refuses, before deleting anything, a project
  whose entry was deleted by hand without that line, and prints the line.
- **Upgrade note:** an instance that removed a project with a production database before this
  release and means to use the name again adds `retired: {<project>: 1}` to
  `catalog/retired.yaml` in the upgrade's pull request.
- `pitr-restore.sh` and `pitr-drill.sh`: a run that fails prints what its scratch copy looked
  like and removes it. Kept "for inspection", it was a failed Job and a database with no
  healthy instance: firing alerts and a red `make verify` on a platform with nothing wrong.
- S01 checks that the new project's production database archives.

## v0.34.26 (2026-10-09) — a new project's three Secrets are copies; a failed point-in-time run no longer blocks the next

- **Upgrade note, an instance with a production database:** the database's bucket credential
  (`orbit-s3` in `<project>-prod`) is now read under the key names the platform's own copies carry,
  `access-key` and `secret-key` (was `HETZNER_S3_ACCESS_KEY` / `HETZNER_S3_SECRET_KEY`). In the
  upgrade's pull request, for each such namespace: `orbit secrets copy . --file
  clusters/platform/secrets/orbit/orbit-s3.yaml --namespace <project>-prod`. The commit gate refuses
  the upgrade until it is done, naming the file and the keys.
- The onboarding run seals `orbit/dev-basic-auth` (the dev door's users line), the copy a project's
  non-prod namespace takes. An instance onboarded before this: `orbit onboard run --only secrets`.
- The operator skill routes "add a service or a project" to the steps (entry, three copied
  Secrets, two desk applies, then the first ship).
- `pitr-restore.sh`: a run that failed marks its scratch namespace, and the next run removes it
  (it told an agent with no kubectl to delete it by hand, and blocked until someone did).
- The scenario suite: a door that gives no answer is asked again (S13, S01); S07 makes a backup
  the platform can restore before it destroys and its teardown puts the games back; the driver's
  GitHub calls are tried three times.

## v0.34.25 (2026-10-08) — the desk asks for its tunnel three times

- One ssh connection dropped by the operator machine's link ended a desk command before it began
  (`the ssh tunnel ... did not open`). The desk now asks for the tunnel up to three times, five
  seconds apart, and a stop repeats what ssh said.
- The scenario suite: reading a component's image and a target's endpoints is asked again on an
  empty answer; a teardown with nothing to ship back to says so.

## v0.34.24 (2026-10-08) — the cluster's DNS forwards only to resolvers it can reach

- A provider's image lists IPv6 resolvers first, the pod network has no IPv6 route, and CoreDNS
  forwards to whatever the kubelet's resolv.conf lists: lookups failed whenever it picked one (285
  times in three hours on the rebuilt test server), which is what failed ships at the registry step.
  `bootstrap-node.sh` gives the kubelet a file of the node's IPv4 resolvers (`--resolv-conf`) and
  restarts CoreDNS when it changes; verify stage2 counts unreachable resolvers in CoreDNS's log.

## v0.34.23 (2026-10-08) — what the memory alert and the sizing advice cannot see, said plainly

- Measured: a .NET service refusing allocations with its container at 56% of the memory limit (the
  runtime caps its heap at 75% of the limit). `OrbitWorkloadMemoryAtLimit` (above 90% for 15
  minutes) cannot see that, and was documented as if it could; `OrbitIngress5xxRateHigh` does.
  The rule's comment and description, PRODUCT.md, the tool's description and the skill now say so.
- `orbit_size_recommend`: a suggestion to LOWER memory ends with what to check first (a runtime that
  sizes its heap from the limit gets a smaller heap, and can already be failing below it).
- S06e plants a workload that is really at its limit (the test tenant's `FILL_MEMORY_PERCENT`).

## v0.34.22 (2026-10-08) — a ship asks the registry again when it could not be reached

- One dropped connection between the cluster and the registry failed a ship at its first step
  (`registry unreachable`). The lookup now asks up to three times when the registry could not be
  reached or answered 429 or 5xx; an answer about the image or the credential is final at once.
- The scenario suite checks that a fault it plants is in the cluster before the agent is asked
  about it.

## v0.34.21 (2026-10-08) — a failed job says in the log where it stopped; the scenarios take their faults back and check

- orbitd logs the step a failed or refused job stopped at, and that step's detail (`orbitd.jobs`); the
  audit line said `failed` and the reason lived only in the in-memory job record.
- The scenario suite: a fault scenario's teardown ships the build back up to three times and fails
  the scenario when the component is still on the fault; a fault scenario refuses to start from
  another one's fault; the traffic helper no longer dies on a request that failed.
- The onboarding skill says how the desk's client signs in after the run.

## v0.34.20 (2026-10-08) — the gate refuses a Secret sealed without the keys its project declares

- A new project's `orbit-s3` (the credential its production database archives with) was sealed under
  the wrong key names; every check passed and the write-ahead log could not be archived. The
  `sealed` check now compares each declared Secret's keys with the key names of the file that seals
  it and refuses by name; the onboarding skill names the two keys.

## v0.34.19 (2026-10-08) — a restore fetches the backup from the bucket when the volume does not hold it

- `orbit_db_restore` (and `scripts/db-restore.sh`) looked for a backup file on the platform's backups
  volume only. The volume is the node's: after a rebuild it is empty, and the copy every backup
  leaves in the bucket could not be restored from. A named backup that is not on the volume is now
  fetched from `s3://<platform bucket>/orbit-backups/<namespace>/`, and `latest` is the newest of
  both places.
- A new instance applies: `instance/config.yaml.example` carries `orbitd: {applyMode: "on"}`. A
  platform onboarded from the example came up with orbitd's applier off, could ship nothing, and had
  no tool to turn it on.

## v0.34.18 (2026-10-08) — `OrbitObjectStoreFailing` does not fire on a Loki that has just started

- On a platform built an hour before, the alert fired for five minutes with the bucket answering
  every request: a new Loki's first chunks are not due for a while, so uploads pause with lines
  arriving. The "uploads stopped" branch now waits until Loki has been up 45 minutes; a request
  the bucket refuses alerts as before, at once.

## v0.34.17 (2026-10-08) — the platform step on a new cluster runs the apply until one pass holds

- On a cluster minutes old the first `orbit platform apply` can stop at an object an admission
  webhook is not yet serving for, and so can the second. The run's `platform` step now runs the
  (idempotent) apply up to five times a minute apart, says so, and keeps the last pass's output as
  `platform-apply.log` in the credentials directory, so a stop can be read.

## v0.34.16 (2026-10-08) — the acceptance run can be read

- The run's acceptance and `onboard status --accept` keep the output of their `make verify` as
  `acceptance.log` in the credentials directory (private), and say so: a run that failed there and
  passed when typed in the same desk could not be read at all.
- The row it was: `the vendored core matches the pin` fetches the pinned core, and the acceptance's
  environment dropped the operator's `GH_TOKEN`, without which a private core repository cannot be
  fetched. The acceptance now hands it on, as the desk does.

## v0.34.15 (2026-10-08) — `orbit onboard run` to the end from a desk: acceptance, resume

- M4's fifth live run built the platform and stopped at its own acceptance while `make verify` typed
  in the same desk passed every stage: the acceptance runner dropped the one variable that tells a
  desk where the node key is, so every node row failed; `onboard status` said `ssh-key-path does not
  point at a file` for the same reason. Both now find the key the way the desk holds it.
- The acceptance of a platform minutes old is run again (three runs at most, five minutes apart);
  only a run with every row green passes.
- A resumed run: `config` no longer reports (and commits) a value it did not change; `tofu init` is
  tried three times before it is a stop, and the stop says how to read why.
- The template ignores `*.log`; the rebuild's cloud-init keeps the provider's own first-boot command.
- **The node takes keys only on ssh.** A rebuilt server came from the provider with a root password
  and password sign-in on, and stayed that way after onboarding. `bootstrap-node.sh` turns password
  sign-in off on every node and asserts it from sshd's effective configuration; verify stage2 has
  the row.

## v0.34.14 (2026-10-08) — the template ships a .gitignore

- M4's fourth live run stopped at `infrastructure`: `git push to the instance repository failed`.
  The template had no .gitignore, and the run's commit took tofu's providers (250 MB, refused by
  GitHub), the state, the variables written from the form and the run's own log. The template now
  ignores what is the operator machine's and not the repository's.

## v0.34.13 (2026-10-08) — `orbit onboard run`: a rebuilt server's root account, and the log as it happens

- M4's third live run rebuilt the server and then waited out its fifteen minutes: the provider had
  set a root password and marked it expired, so the node key was accepted and every command
  refused. The rebuild's cloud-init now keeps the password from being marked expired.
- The run's step lines reach a log file as they are printed (they sat in a buffer until the run
  ended, and the agent watching read an empty file). The onboarding skill says how to wait.

## v0.34.12 (2026-10-08) — `orbit onboard run` on a repository whose main takes pull requests only

- M4's second live run stopped at `repository`: `git push to the instance repository failed`. The
  organization's repository keeps direct pushes off main (a ruleset: "Changes must be made through
  a pull request"). The run now takes the path the rule asks for (a branch, a pull request, a merge,
  and the checkout follows), and the `github-app` step adds Orbit's App to the bypass list of a
  repository ruleset that keeps pushes off main, because Orbit writes there as the App; it says
  which ruleset it changed. A rule on the organization, or one that wants a review, is a stop in
  one sentence.

## v0.34.11 (2026-10-08) — `orbit onboard run` on a Windows operator machine: the form is made private by the run

- M4's first live run stopped before its first step: `form.yaml is readable by others (mode 777);
  chmod 600 it first`. A credentials directory on a Windows machine reaches the desk as a mount
  where everything reads as 0777 until narrowed from inside the desk, and a chmod typed on the
  Windows side changes nothing. The run now narrows the form, the credentials directory and the
  GitHub App file to their owner itself, says so, and refuses only what it could not narrow.

## v0.34.10 (2026-10-08) — verify asks again when a connection was not made; the minimal profile measured

- `make verify` turned a healthy box red twice in a row, a different row each time: one dropped
  connection on the operator machine's own link (a control address dropped at the same moments).
  The http-to-https redirect row asks again, twice, when it got no answer at all (`000`), and the
  node ssh rows let ssh retry the connection (`ConnectionAttempts=3`). An answer that is wrong is
  judged at once, as before.
- `docs/PRODUCT.md`: the minimal profile's measured memory (every optional feature off): 648 MiB,
  601 to 663 over 6 hours.

## v0.34.9 (2026-10-07) — `orbit onboard run` from the desk on a real machine (docs/V1.md M3)

- Read before the first live run, four things that would have stopped it: the key pointer
  (`ssh-key-path`) named the key's path inside the desk container, where the operator machine has
  no such path (the desk now says where the credentials directory really is,
  `ORBIT_HOST_CONFIG_DIR`); the run never opened the ssh tunnel to the node's API, which the desk
  only opens when a node already exists at its start; a node key in a directory mounted from
  Windows reads as mode 0777 and ssh refuses it (the run uses a private copy); and a checkout with
  no git identity could not commit (the run commits as itself then).

## v0.34.8 (2026-10-07) — scenario S01 onboards any project; the default-size chart test (docs/V1.md M4, M4b)

- S01 takes `S01_PROJECT` and `S01_KEEP=1`, and its teardown removes the project through
  `orbit_project_remove` (it used the desk script, around orbitd). A chart test holds the default
  size of a component that names none. No change to orbitd or the platform tree.

## v0.34.7 (2026-10-07) — the onboarding skill says how to run a run that outlasts one command (docs/V1.md M3)

- `orbit-onboard`: the run takes 40 to 60 minutes and outlasts one command; the skill says to
  start it writing to a log, read the log until it ends, and never run a second one beside it.

## v0.34.6 (2026-10-07) — what `make verify` found on the minimal profile (docs/V1.md M2c)

- `orbit platform plan`: each of its workers has a cluster client of its own. One client shared by
  six threads failed with `KeyError: 'api'` once a retired feature's kinds were looked up (the
  library rebuilds its discovery cache then, and it is not safe under concurrent lookups).
- `make verify` stage5: the Loki and Tempo dial rows are on when the feature is on and the app was
  applied. A retired app keeps its stored inventory, and with traces off the stage dialled a Tempo
  that was gone ("Tempo accepted ZERO spans").
- Removing a project (`orbit_project_remove`, `catalog-commit.sh remove`) also removes its sealed
  Secret files (`clusters/platform/secrets/<project>-<env>/`) in the same commit; left behind,
  `make verify` stage4 asked the cluster for Secrets of namespaces that were gone.

## v0.34.5 (2026-10-07) — the databases feature can be turned off (docs/V1.md M2c)

- Turning `databases` off failed every time: retiring the database operator would garbage-collect
  the two certificates it makes for itself, and the retire guard refuses to collect any Secret.
  A feature may now name such Secrets (`generated:` in `provisioning/features.yaml`); a retire
  lets exactly those go, says so, and still refuses for any other.

## v0.34.4 (2026-10-07) — sizing advice; a cron's failed run no longer rings for ever (docs/V1.md M4b, M2c)

- Sizing advice (docs/V1.md M4b). `orbit_size_recommend(project, env, window_days=7)`, a viewer
  tool that changes nothing: per running component, what memory and CPU it asks for, what it used
  (peak and median working set; p95, mean and peak CPU), and a suggested request and limit with a
  verdict (`lower`, `raise`, `leave`, `no_suggestion`) and the reason in one sentence. Memory is the
  peak plus 15%, CPU the p95 plus 15%, an out-of-memory kill raises memory, and a component that
  sits at its memory limit without being killed gets no suggestion and is told so. A suggestion is
  applied with `orbit_catalog_set`. New alert `OrbitWorkloadMemoryAtLimit` (warning): a tenant
  container above 90% of its memory limit for 15 minutes, labelled with project, env and component;
  kube-state-metrics now also exports the `orbit.local/env` namespace label for it.

- A cron component's finished run is deleted after 20 minutes (`ttlSecondsAfterFinished`, a
  component may set its own). One failed run used to keep `KubeJobFailed` firing until three later
  runs had failed too; a run cut by a node reboot rang for hours.
## v0.34.3 (2026-10-07) — the scenario runner cleans up after a failed setup (docs/V1.md M2c)

- Scenario runner: a setup that fails still runs its teardown (a dead setup had left a fault build
  and a traffic loop behind for hours). S16 also checks that every database cluster has all its
  instances ready. The watchers driver makes its ticker pod fresh and stops when it is not running.
- No change to orbitd or the platform tree since v0.34.2.

## v0.34.2 (2026-10-06) — what the suite on v0.34.1 found (docs/V1.md M2c)

- A job waits for the checkout to CARRY its commit, not to be exactly at it. When a second commit
  landed within the minute the checkout takes to follow the repository, the first job waited out
  its nine-minute deadline holding its lock and failed with "checkout lag" (ship, rollback, restart,
  monitor upsert and delete, catalog set, feature set). Two agents at once is the ordinary case.
- `orbit_monitor_delete` cleans up after a delete that did not finish: a name gone from the
  repository whose rule is still in the cluster is confirmed naming the objects and pruned
  (`commit` skipped, `evidence.left_behind`). Nothing else prunes a monitor's objects.
- The scenario runner fails a scenario whose teardown fails; S10a and S10b's teardown checks the
  monitor's rule is really gone.
- `orbit_monitor_upsert` says who hears the monitor fire: its description and the `orbit-operator`
  skill name `notify`, and the answer carries `evidence.reaches` ("sent to the human chat" or
  "recorded by orbitd and nobody is messaged"). An agent asked for a monitor that alerts had made
  a silent one. S10a now proves its own alert reached the human channel (the delivery recorder
  names it); the notification counters alone had let a silent monitor pass.
- The desk reopens its ssh tunnel to the node when it finds it closed (checked every 5 s). One
  dropped connection in the middle of `make verify` had turned every later row red for a
  healthy platform.

## v0.34.1 (2026-10-06) — the platform plan fits inside an agent's command (docs/V1.md M2c)

- `orbit platform plan` plans the apps side by side (six at a time, `ORBIT_PLAN_WORKERS`) and
  reports them in declared order. Measured from the operator machine on fifteen apps: 845 s
  one after another, 209 s now. The agent upgrading core to v0.34.0 merged the pin and never
  saw its plan, because the plan outlived the ten minutes its command may run.
- `orbit-ship` (upgrading core) says how long the plan takes and to wait for it.
- Comments in the template that still said CI builds images or writes tags say who does.

## v0.34.0 (2026-10-06) — features, the commit gate, guardrail tools (docs/V1.md M2c)

- Features (docs/V1.md M2c): `provisioning/features.yaml` names what the platform is made of.
  `core` always runs; `logs` (Loki, Alloy) and `traces` (Tempo, the OpenTelemetry operator)
  are optional and switched under `features:` in `instance/config.yaml`. Without that block
  nothing changes. With it, a feature left out is off: its Applications, network policies,
  Grafana datasource, alert rules and (for traces) Traefik's trace export and the tenant
  `Instrumentation` objects leave the rendered tree. `make verify` stage5 is red when a
  feature is on with an app not applied, or off with one still applied.
- orbitd follows the features: a tool owned by a feature that is off is not in `tools/list`
  and a call to it returns `FEATURE_OFF` with how to turn the feature on; an open
  `subscriptions/listen` stream is told `tools/list_changed` when the set changes. With
  logs off, `orbit_logs_query` and the deploy step's log check read the node's pod logs
  through the Kubernetes API (current and previous container runs; line filters only).
- Switching a feature: `orbit_features_set` (admin, confirmed) commits the switch with ledger
  trailers and converges the cluster in a Job (`orbit features converge`: apply, re-apply
  consumers with prune, retire, wait ready); `orbit_features_list` and `GET /api/features`
  give each feature's state (declared, applied, ready, what it requires, its tools, its
  memory now). Desk: `orbit features list|enable|disable` through orbitd, `orbit features
  status|set|converge` directly. `orbit onboard status` gains a `features` step. New skill
  `orbit-features`. The instance's `platform-config` gains the `orbit-platform`
  ServiceAccount (cluster-admin, used only by the converge Job) and lets orbitd read
  SopsSecret objects.
- More features: `dashboards` (Grafana, by a chart value), `uptime` (the blackbox prober,
  the platform's and each project's probes, the two public-surface alerts), `databases`
  (the CloudNativePG operator, the database alerts, `orbit_db_status`), `backups` (the
  barman-cloud plugin and a prod database's bucket archive; needs a bucket and
  `databases`) and `alerts-telegram` (the Telegram notifier; off, alerts are still raised
  and recorded, and `contacts.telegram` is not needed). What still needs a feature that is
  off is refused before anything is written: a project that declares a database, a probe
  monitor or `monitoring.probes`, a monitor that notifies telegram. An instance whose
  `features:` block was written before these existed must name them (`orbit features set`)
  or they are off after the upgrade; the commit gate refuses the upgrade commit if that
  would drop something a project needs.
- An agent no longer needs a shell on the operator's machine for these, and each is behind
  a role, a confirmation and the audit log:
  - `orbit_catalog_set` (operator): replace one project's catalog entry (replicas, memory,
    a per-environment override, hostnames, components) in one commit by orbitd; every
    other line of `catalog/projects.yaml` stays as it was (`scripts/catalog-edit.py`), the
    generator is the judge of the entry before anything is pushed, and the commit gate
    judges the commit before anything is applied.
  - `orbit_db_backup` (operator): dump a database, restore the dump into a scratch database
    on the same server, compare every table's row count, keep the file on the platform's
    backups volume (claim `orbit-backups` in namespace `orbit`, made on first use) and copy
    it to the bucket when there is one. A dump that does not restore is not a backup.
  - `orbit_db_restore` (admin): a backup file or, for prod with `backups` on, a point in
    time; `dry_run` compares without changing anything; the live state is backed up first
    and a backup that cannot be proven refuses the restore.
  - `orbit_project_remove` (admin): the catalog entry, then every database backed up and
    proven, then the namespaces; a backup that is refused deletes nothing.
  They run core's own scripts (`scripts/db-backup.sh`, `scripts/db-restore.sh`,
  `scripts/remove-project.sh`, from the desk image of the same release, never the instance
  repository's copy) in a Job in namespace `orbit`. `scripts/remove-project.sh` now proves
  each backup by restoring it, as the tool does. New skill `orbit-change-service`;
  `orbit-restore-db` is rewritten around the tool. Onboarding seals `orbit/orbit-s3` (the
  bucket keys for those Jobs); an existing instance copies it from `loki/orbit-s3`
  (`orbit secrets copy`). orbitd's deployment gains `ORBITD_DESK_IMAGE`.
- A ship runs this build's own scripts: orbitd (and the desk) run their own copy of
  `bump-and-push.sh` on the fresh clone, and nothing out of the clone runs unless its
  vendored core is byte for byte the build's (`scripts/core-same.py`); a clone pinned to
  another core is refused with what to do. The commit gate's `core` check and the ship
  check also refuse a Python module planted beside core's scripts (a `scripts/yaml.py`
  would have been the `yaml` the generator imports).
- The generator refuses, by name, a component called `db-backup` beside a database (the
  chart's own backup job has that name) and a key in a project entry that nothing reads.
- Onboarding chooses the features (docs/V1.md M3): the form takes `features:` (one left
  out is off; none at all is core alone), and `object_storage` and `telegram` are optional,
  needed only by the features that use them. A feature turned on without what it needs
  is refused before the first step, naming the form field. The run writes the instance's
  `features:` block, drops the example `objectStorage` block for a platform with no
  bucket, seals only the secrets the chosen features need, and skips the buckets step
  without object storage. `provisioning/features.yaml` carries each feature's measured
  memory (`memory_mib`), which `skills/orbit-onboard` shows as a table.
- Onboarding fixes found by running the form the whole offline way (template, config,
  `make init`): the run now fills `coreRegistry.owner` (the registry of the core the
  instance pins), drops the example `agentRuntime` and `intakeRuntime` blocks and the
  unused admin-name lines, and seals `monitoring/alertmanager-intake` (Alertmanager mounts
  it on every platform). Before this the scaffolder refused the filled config.
- Found by the live proofs on the test box and fixed: a restore asked for right after its
  comparison waited on nothing and was refused while the comparison's throwaway namespace
  was still terminating (it now waits for it); a job whose work was a dry run read
  `failed` in `orbit_job_wait` (it completed); a feature whose workload was scaled to 0
  read "applied and ready" in `orbit features status`, `orbit_features_list` and `orbit
  onboard status` (it is NOT READY, named, "(scaled to 0)"); the acceptance harness's
  confirm proxy left its host waiting half an hour for an answer the ingress restart had
  dropped (it now ends a call after 200 s with a tool error `answer_lost`). Switching
  `traces` restarts the ingress once; `skills/orbit-features` says what a wait in flight
  then looks like and what to do.
- Scenarios S19 (an admin agent switches a feature off and on), S20 (a viewer asks for what
  an off feature owns), S17b (a removal whose backup cannot be proven deletes nothing) and
  the live gate driver `scenarios/obs/gate.sh` (one bad commit per check). The agents of
  S07, S09 and S17 hold orbitd's tools and no shell.
- The commit gate: orbitd runs a chain of checks on every new commit of the instance
  repository before applying anything from it (`core`, `scaffold`, `catalog`, `generated`,
  `render`, `sealed`, `fit`). A commit that fails is refused: the last good commit keeps
  running, `orbit_deploy_status` shows the failing check and its sentence under `gate`,
  and the alert `OrbitCommitRefused` fires until a commit passes. `orbit check all <dir>`
  runs the same chain at the desk. Both images now carry their own copy of orbit-core
  and the checks run only that copy's scripts. Build the desk image with
  `--build-arg ORBITD_VERSION=<tag>` as well.
- Every platform manifest is rendered by `make init` on every run: the files that were
  copied once from `template/clusters/platform` moved into the staged tree, so an upgrade
  now delivers their changes (an instance created earlier still carried old copies,
  among them a monitoring network policy without the exporters' ingress rule).
- A guardrail Job that failed and was read is deleted. A removal refused for want of a
  provable backup (the tool working as it should) left a failed Job in the `orbit`
  namespace, and `KubeJobFailed` fired for the day the Job is kept, for something the
  caller had already been told. orbitd now writes the script's last lines to its own log
  (`orbitd.desk`) and deletes the Job once it has read the outcome; a Job orbitd never
  got to read stays and still alerts. S17b checks that no failed removal Job is left.
- `scenarios/run-all.sh` runs S17b, S19 and S20 too (they were added without being added to
  its list); `scripts/test_scenarios_suite.py` fails when a scenario is left out.
- No CI (owner's rule): every GitHub Actions workflow is removed from core. The offline tests
  run on the operator machine with `scripts/local-tests.sh` (a Linux container built from the
  desk image).
- The optional GitHub Actions build pipeline left the template
  (`template/.github/workflows/build-service.yml` and `template/.github/app-repo-build.example.yml`):
  images are built on the operator machine. `make core-sync` removes an instance's
  `.github/workflows/build-service.yml`, and `make init` no longer maintains a `.github/` tree.

## v0.33.9 (2026-10-05) — the object-store alert sees a bucket that stops answering (docs/V1.md M2b)

- `OrbitObjectStoreFailing` also fires when Loki has made no successful upload for 15
  minutes while lines keep arriving: a bucket that does not answer at all leaves Loki's
  request counter silent, failures included.
- The §6.4 driver blocks the bucket by network policy with Loki running; the §6.6 driver
  frees the pod quota before its series test and fails loudly when its target never ran.

## v0.33.8 (2026-10-05) — onboarding adopts a server by rebuilding it in place (docs/V1.md M4)

- `orbit onboard run` with `hetzner.server_id` requires `hetzner.rebuild: true`: the server
  is rebuilt through Hetzner's API (same server and address, no spend) with cloud-init
  authorizing the run's node key, once; the DNS wildcards, firewall and ssh key an
  earlier life left behind are imported into tofu (`tofu/adopt.tf`), so DNS is unchanged.
- The §6.4 driver's queries and alert waits work on a Windows operator machine.

## v0.33.7 (2026-10-05) — the database-down alert sees a stopped Postgres (docs/V1.md M2b)

- `OrbitDatabaseDown` fires when CloudNativePG's exporter cannot reach Postgres or when a
  database pod's postgres container is not ready (a fenced database kept the exporter's
  `cnpg_collector_up` at 1).
- `scripts/test_catalog_no_tag.py`: the generator writes no tag before the first ship.
- Scenarios: `catalog_append` follows the catalog's list indentation; the §6.3 driver waits
  one Alertmanager group_interval for a grouped firing notification and runs the node
  reservation class alone.

## v0.33.6 (2026-10-05) — the alert recorder keeps every label (docs/V1.md M2b)

- The alert recorder writes every label of each delivered alert (never annotations), so a
  delivered alert can be told from its siblings by the pod, service or job that raised it.
- Scenarios: S01, S08a, S09 and S17 agents may Edit and Write the instance checkout; S14
  accepts the reconcile loop's revert record as the drift report.

## v0.33.5 (2026-10-05) — onboarding in one run; the reboot finding (docs/V1.md M0, M3)

- `orbit onboard run <dir> --form <form.yaml>`: one private form and two GitHub App clicks,
  then every onboarding step in order, idempotent, values file to file, ending with a
  Recovery Kit. `scripts/desk.sh` publishes loopback ports named in `ORBIT_DESK_PORTS`.
- `skills/orbit-onboard` leads with the run; docs/ONBOARDING.md makes the Actions build
  pipeline optional (the default build is local).
- `OrbitPlatformMemoryOverBudget` judges the 15-minute average of platform RSS, held 20m: a
  reboot's minutes of WAL replay put the bare sum over budget (pending, never firing).
- Scenario S16 hands over to its agent only once orbitd's public door answers.

## v0.33.4 (2026-10-05) — the third scenario round and the observability suite (docs/V1.md M2, M2b)

- orbitd's namespace admits ingress only from traefik and monitoring (a tenant pod could
  reach orbitd).
- The reconcile tick records the fields a hand edit changed and `orbit_deploy_status`
  returns them as `recent_hand_edits`.
- Catalog components take a per-environment `env` override, merged key by key (null removes
  a key).
- `scripts/wait-applied.sh <project> <env>`: blocks until orbitd's inventory for the pair
  carries the pushed commit (headless agents cannot sleep).
- Observability: an in-cluster alert recorder beside the Telegram receiver; every database's
  PodMonitor on; project/env/component labels on ServiceMonitor targets and on Alloy's log
  streams; a 5000-sample scrape limit with `OrbitScrapeSampleLimitHit`; `OrbitLogsDiscarded`;
  Tempo's span metrics carry exemplars; Loki logs at warn (its info level wrote query text).
- `make verify` stage7-scenarios: the guardrails probed with throwaway principals, tenant
  isolation, and every public door's http→https redirect.
- Every tracked shell script is mode 100755, and `scripts/test_script_modes.py` keeps it so
  (a Windows commit had dropped the exec bit; the vendored `remove-project.sh` would not run).
- Scenarios: the §6 drivers (`scenarios/obs/`), `scenarios/run-all.sh`, and S10b, S12, S17, S18
  corrections.

Upgrade note: `make core-sync` on Windows git (`core.filemode=false`) does not record the exec
bit; after the sync run `git ls-files -s -- '*.sh' | grep 100644` and
`git update-index --chmod=+x` each one it lists, once.

## v0.33.3 (2026-10-05) — what the second scenario round found (docs/V1.md M2)

- The tenant applier reverts hand edits server-side apply cannot undo: fields a hand-edit
  field manager (kubectl, k9s, the consoles) owns alone are removed after each apply, by
  keys, with one resourceVersion-guarded JSON patch; the dry run and the reconcile summary
  name them.
- `scripts/pitr-restore.sh` keeps owners and grants when it loads the restored data, and
  reports RESTORED only when every relation belongs to the database's owner (a restore
  left the application unable to read its own tables).
- orbitd's post-ship door check requests the component's own door (path + probePath), not
  the host's root.
- The derived quota counts the chart's pg_dump Job for a database with no bucket (a new
  project's database could not bootstrap beside it).
- `make verify` stage5 judges a catalog component's bare sibling Service name in its project
  namespaces.
- Skills: upgrading orbit-core and forcing a certificate renewal (instance skill orbit-ship),
  routed from orbit-operator.
- Scenarios S01, S02, S05, S08a/b, S09–S18 (`scenarios/`); `scenarios/lib.sh` runs on a
  Windows operator machine and cleans up after a failed helper.

## v0.33.2 (2026-10-05) — what the scenario suite found (docs/V1.md M2)

- `scripts/remove-project.sh`: the supported path for removing a project dropped from the
  catalog — refuses while it is still catalogued, dumps and verifies every database table
  by table before any delete, then prunes, deletes its namespaces and monitoring objects
  and proves nothing labelled with it is left.
- `scripts/pitr-restore.sh` is vendored: point-in-time restore of a prod database from the
  bucket, dry run first; operator skill `orbit-restore-db` drives it.
- `orbit_deploy_ship` answers "busy" with the reconcile holder and a retry hint; post-ship
  log assertions on a reused pod read only its recent window.
- `make init` no longer refuses core-sync's own uncommitted, identical output (an upgrade
  is `make core-sync && make init` again).
- `make verify` stage3 probes a protected dev door at its door path, as stage2 does.
- Scenarios S04, S06a–d, S07, S17 (`scenarios/`), each with an independent check.

Upgrading an existing instance: the template `Makefile` is not vendored; copy its `verify`
target if yours still stops at the first failing stage.

## v0.33.1 (2026-10-04) — what the first fresh-agent runs and the first prod ship found

- Tenant probes and `make verify` stage2 check a project's door where it answers: the root
  when some web component serves `/`, else the first web path plus its probePath.
- `orbit_deploy_status` judges a cron component by its CronJob template, not by an absent pod.
- The generator writes no `tag` before the first ship (the chart then idles the component),
  and the derived ResourceQuota counts a WAL-archiving database's barman sidecar.
- Alert rules: every platform rule has a promtool unit test in CI; new rules
  OrbitDatabaseDown, OrbitIngressLatencyHigh, OrbitLogsNotArriving, OrbitObjectStoreFailing.
- Skills: what a new project needs beyond its catalog entry (per-namespace sealed Secrets,
  the two desk applies, a first ship).
- The scenario harness (`scripts/skills-acceptance.sh`) runs on a Windows operator machine;
  `scenarios/run.sh` runs an operating scenario with a fresh agent and an independent check.

## v0.33.0 (2026-10-04) — the V1 run's first release (docs/V1.md M0/M1)

- The desk runs from a container: `scripts/desk.sh <command>` (desk/Dockerfile) carries
  kubectl, helm, sops, age, promtool, tofu and the `orbit` CLI, so the operator machine
  needs only Docker and git.
- CI: one `tests` workflow runs the whole scripts suite and orbitd's suite under pytest.
- `OrbitPlatformMemoryOverBudget` counts RSS, not working set (a reboot's page cache no
  longer pages anyone).
- orbitd `/healthz` reports the release it was built from.
- `make verify` stage6-live: the live platform is the committed one (plan empty, running
  orbitd is the pin, `/healthz` reports the pin).
- Shipping a `kind: cron` component lands when its CronJob carries the tag (it always
  failed the rollout step); the desk client waits as long as `orbit_job_wait` may block;
  a component with no tag yet runs nothing (replicas 0, a suspended CronJob) instead of
  pulling a `:latest` that does not exist.
- `orbit platform plan` treats an empty env value as the API server dropping it (no more
  permanent drift on `ORBITD_APPLY_PAIRS=""`).
- Skills: build images on the operator machine from a clean, pushed commit (operator
  `orbit-ship` step 0); Actions builds are optional.
- Docs: superseded records removed (index in docs/README.md), measured sizing in
  PRODUCT.md, this changelog.

## Released

- **v0.32.1** (2026-09-16) — arch-node r1: the fake-node sandbox owns /usr/lib/systemd and /run/systemd; arch-node: bootstrap on Arch/Omarchy, owned-node provider, 6443 host rule
- **v0.32.0** (2026-09-15) — sweep area 6: the desk trusts no in-band marker, frames every operator line, keys sessions on origin; sweep area 5: charts/service schema anchoring, quoted render sites, template netpol; orbitd: adversarial sweep area 4 (tools, operator and viewer tools); orbitd sweep area 3d: plan output carries Secret fingerprints only; helm argv/paths shape-checked and bounded; webhook and bootstrap hardened
- **v0.31.6** (2026-09-13) — verify: area-1 adversarial sweep, 26 rounds of repairs (stages, loader, generator)
- **v0.31.5** (2026-09-12) — Onboarding as an agent-driven skill and `orbit onboard status`
- **v0.31.4** (2026-09-12) — monitoring: diet three -- the collector's target and one Grafana sidecar
- **v0.31.3** (2026-09-12) — orbitd: shape schema-refused arguments into the envelope; skill sentences from throwaway round two
- **v0.31.2** (2026-09-12) — desk, metrics, skills: what the stranger session found on v0.31.0
- **v0.31.1** (2026-09-12) — monitoring: series diet round two, the API-server SLO groups off (phase 8); platform: the dashboard server keeps no copy of its sign-in; fix(orbitd): the panel checker reads the client's objects, not only dicts
- **v0.31.0** (2026-09-12) — one screen notation for the desk, the audit record's floor keeps every own field, operator skill's open code list and job shape, reference document names orbitd as the applier
- **v0.30.0** (2026-09-10) — v0.29.0's deploy blockers fixed and the phase 8 series diet
- **v0.29.0** (2026-09-10) — monitors end to end, secrets.provider: sops, orbit secrets mint
- **v0.28.2** (2026-09-10) — a dry-run prune never deletes
- **v0.28.1** (2026-09-10) — tenant inventory retention
- **v0.28.0** (2026-09-10) — ArgoCD removed from the template
- **v0.27.2** (2026-09-10) — platform apply omits render stamps
- **v0.27.1** (2026-09-10) — tenant live-kinds list restored
- **v0.27.0** (2026-09-10) — orbit platform apply
- **v0.26.1** (2026-09-10) — the apply switch renders quoted; dev is orbitd-applied
- **v0.26.0** (2026-09-10) — the applier is switched on per project/env
- **v0.25.0** (2026-09-10) — orbitd can apply the tenant sets itself (switch off by default)
- **v0.24.0** (2026-09-10) — the shadow render covers the whole generated tenant tree
- **v0.23.3** (2026-09-10) — hooks: explicit null on rollingUpdate so a live Deployment can move to Recreate
- **v0.23.2** (2026-09-10) — provisioning: vendor Traefik's own CRDs (wave 0) and render monitoring.probes from config
- **v0.23.1** (2026-09-10) — orbitd: the shadow diff is empty against the live cluster's defaults; ship: take the landed commit from the clone's HEAD; bump failures carry the exit code; orbitd: deploy_status reads running images from the pod spec
- **v0.23.0** (2026-09-10) — the shadow render-and-diff
- **v0.22.1** (2026-09-10) — instance: vendor the provisioning source tree; orbitd namespace on the prod store allowlist; catalog: admit orbitd to non-prod namespaces on the web ports; cli: orbit call exits non-zero on any error payload
- **v0.22.0** (2026-09-10) — the operator deploy path through MCP
- **v0.21.0** (2026-09-10) — viewer tools, instance checkout, backend netpols, confirmed CLI calls; orbitd live
- **v0.20.0** (2026-09-09) — orbitd backends, CLI and platform manifests; phase 2 accepted
- **v0.19.3** (2026-09-09) — phase 1 accepted live; web check sends basic auth
- **v0.19.2** (2026-09-09) — Instrumentation resources under the CRD's per-language field
- **v0.19.1** (2026-09-09) — derived quota charges the instrumentation init container
- **v0.19.0** (2026-09-09) — ship from the desk, four post-ship assertions, catalog quota, drain/scratch, overrides, orbitd core
- **v0.18.3** (2026-09-08) — scratch volume and readiness path in the service chart
- **v0.18.2** (2026-09-08) — the no-direct-push rule with the deploy-bot carve-out in every vendored skill; root .gitignore. PR #41, #42.
- **v0.18.1** (2026-09-07) — provisioning can log in as the admin the root ceremony creates: plain password login with SRP fallback; secrets.adminEmail renders the vault admin. PR #40.
- **v0.18.0** (2026-09-07) — the in-cluster vault is the only secret source: provider setting real, Kubernetes-auth stores, tofu vault module, seed path, stage4 rebuilt with pre-boot key-off, in-cluster onboarding by default. PRs #37, #38, #39.
- **v0.17.0** (2026-09-07) — ready for a second org: front door, clean skills, vendored platform verify, tenant CI
- **v0.16.0** (2026-09-07) — the template catches up with the reference instance
- **v0.15.0** (2026-08-31) — the node's inotify instance budget fits what runs on it
- **v0.14.0** (2026-08-31) — a web Service declares the container's port too, not only 80
- **v0.13.1** (2026-08-31) — the example config must be a config that scaffolds
- **v0.13.0** (2026-08-31) — a component may declare a burst ceiling above its reservation
- **v0.12.2** (2026-08-31) — init preserves a store's conditions allowlist
- **v0.12.1** (2026-08-31) — retire the deploy-on-build concurrency row
- **v0.12.0** (2026-08-30) — graceful roll for web components
- **v0.11.0** (2026-08-30) — gate github triggers on intakeIngress:wired; stage3 asserts the public webhook door is path-scoped (exact backend orbit-agents-intake, no defaultBackend, no tls, rc-guarded reachability)
- **v0.10.2** (2026-08-29) — two live-only findings: intake routes checksum (stale routes) + agent-plane exemption from Alertmanager inhibition
- **v0.10.1** (2026-08-29) — the plane-route oracle mutates the GENERIC route
- **v0.10.0** (2026-08-29) — feat(agent-plane): metric & logs triggers — rule + reused intake route from one catalog block (#11)
- **v0.9.0** (2026-08-29) — Phase D.3 intake service: event router (triggers, per-source auth, dispatch to Jobs/warm agents)
- **v0.8.0** (2026-08-29) — Phase D.3 (cron): mode:cronjob renderer + runtime one-shot entrypoint + SIGTERM handler (2-vote)
- **v0.7.0** (2026-08-28) — Phase D.2: catalog agents: → agent plane (2-vote approved, tightened)
- **v0.6.6** (2026-08-29) — stage2 public-surface enumerator: parse customDomains mappings, fail closed
- **v0.6.5** (2026-08-28) — stage4 sanctions the masterkey recovery file by name; stage3's instance-string leak check actually runs (was failing open)
- **v0.6.4** (2026-08-27) — agent-runtime: default model = claude-sonnet-4-6 (cheapest capable tier; per-agent override stays)
- **v0.6.3** (2026-08-27) — AGENT-PLANE: execution plane revised — kagent dropped (pre-1.0, not GitOps-consumable), agents run on our A2A image + CronJobs/intake (the owner approved 2026-08-27)
- **v0.6.2** (2026-08-27) — D.4: the notifier — CNPG outbox + Telegram adapter (notify + interactive-approval)
- **v0.6.1** (2026-08-27) — D.1: the agent runtime image — A2A over the Claude Agent SDK (subscription token, never an API key)
- **v0.6.0** (2026-08-27) — Phase C template parity + the provisioning runbook (executed, not designed)
- **v0.5.6** (2026-08-27) — netpol: admit the MCP plane into project namespaces (RO postgres MCP)
- **v0.5.5** (2026-08-27) — bootstrap: accept-new host keys — first contact with a seconds-old server cannot be strict
- **v0.5.4** (2026-08-27) — instance-init: named failure for bucket-without-endpoint (found by the fresh-org rerun)
- **v0.5.3** (2026-08-27) — orbit-observe: the Phase 4 instrumentation rules (Decision 7)
- **v0.5.2** (2026-08-27) — instrumentation: OTEL_PYTHON_LOG_CORRELATION on — the SDK half of logs<->trace
- **v0.5.1** (2026-08-27) — stage3: OTel identity/injection render checks, both directions
- **v0.5.0** (2026-08-27) — B.4: connected traces are a catalog fact (v0.5.0)
- **v0.4.7** (2026-08-27) — template kps: the receiver+3Gi block that v0.4.6's patch dropped
- **v0.4.6** (2026-08-27) — template parity for B.3: tempo (tokenized), traefik root spans + $USER, kps datasources/links/receiver/3Gi; endpointHost token; bucket-less prune covers tempo
- **v0.4.5** (2026-08-27) — WAL-stall signal is the .ready backlog, not archival age — idle databases go quiet legitimately (false-red observed live)
- **v0.4.4** (2026-08-27) — template parity for Phase B: loki (tokenized bucket/endpoint), alloy, obi + loki ES; instance-init prunes bucket-backed apps for bucket-less orgs (OBI stays — no bucket needed)
- **v0.4.3** (2026-08-27) — stage3: dashboards get their own assertion, not a silent skip in the render check
- **v0.4.2** (2026-08-27) — dashboards: explicit panel ids — provisioned JSON is kept verbatim, unaddressable without them
- **v0.4.1** (2026-08-27) — generator: one RED dashboard per project+env from OBI semconv metrics (Decision 6)
- **v0.4.0** (2026-08-27) — the switch: with s3, the pg_dump path does not render (v0.4.0)
- **v0.3.10** (2026-08-27) — plugins: write the enabled default out — omitted, Argo is permanently OutOfSync
- **v0.3.9** (2026-08-27) — drill: reference table must have rows — 0==0 is a vacuous pass, found live on an empty leaderboard
- **v0.3.8** (2026-08-27) — drill: copy the source namespace's real NetworkPolicy — a bare deny rejects the operator's status probe
- **v0.3.7** (2026-08-27) — drill: pin to an emitted WAL marker — a boundary LSN is never passed and a time target needs commits; both FATALed in execution
- **v0.3.6** (2026-08-27) — drill: pin recovery by LSN — a time target is never reached on a quiet database (PG17 FATALs at end-of-WAL)
- **v0.3.5** (2026-08-27) — manifest: vendor pitr-drill.sh into instances
- **v0.3.4** (2026-08-27) — ObjectStore sidecar: 192Mi — base backups stream through it; 64Mi OOM-killed (exit 137) mid-backup
- **v0.3.3** (2026-08-27) — ObjectStore: sidecar resources — the namespace quota rejects any container without them (DB pod deleted, replacement forbidden)
- **v0.3.2** (2026-08-27) — WAL archiving via the barman-cloud plugin — the in-tree path is dead on -standard- images
- **v0.3.1** (2026-08-27) — schema: helm validates the chart's own default values — s3 completeness via draft-07 dependencies, not required
- **v0.3.0** (2026-08-27) — WAL archiving to the platform bucket for prod databases (B.1)
- **v0.2.10** (2026-08-27) — stage3: only self-hosted, component-bearing projects expect namespaces
- **v0.2.9** (2026-08-27) — stage3: population expectations derive from the catalog, not the reference shape
- **v0.2.8** (2026-08-27) — stage7 is tenant content (tenant-a) — leaves core, stays instance-owned
- **v0.2.7** (2026-08-27) — stage2: catalog-driven public surfaces — the tenant-a/apex pins move to the instance
- **v0.2.6** (2026-08-27) — bootstrap: kubelet reservations scale with node RAM
- **v0.2.5** (2026-08-27) — tofu: create-capable server, subzone-aware DNS, dev wildcard
- **v0.2.4** (2026-08-27) — Verify: pre-boot and zero-tenant keying (declaration-based, never reachability)
- **v0.2.3** (2026-08-27) — instance-init: scaffold tokens are dotted+space-free; stop tripping on Go templates
- **v0.2.2** (2026-08-27) — Template pin -> v0.2.2 (instance-init vendored)
- **v0.2.1** (2026-08-27) — Commit the executable bit on scripts — vendoring propagates git's mode
- **v0.2.0** (2026-08-26) — orbit-hooks: the webhook receiver (Phase A item 3)
- **v0.1.2** (2026-08-25) — fix: stage3 label-key schema was wrongly genericized (owner.example/ prefix is API schema, not an instance string)
- **v0.1.1** (2026-08-25) — stage3: vendored-core drift check (keys off in the core repo itself)
- **v0.1.0** (2026-08-25) — orbit-core v0.1.0 — extracted from the founding instance
