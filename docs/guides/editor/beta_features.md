# Beta Features

Beta features are new features that aren't ready to be on for everyone yet.
You can turn them on to try them early, and turn them off again at any time.
They're managed from the **Beta Features** page in the editor's settings.

<!-- screenshot: the Beta Features settings page with Editor and Engine groups and a few toggles -->

## Turning a beta feature on or off

Open **Settings → Beta Features**. Each feature is listed with a short
description of what it changes and where, and a toggle to turn it on or
off. Your choice is saved with the rest of your settings, so it stays
the same the next time you open the editor.

Features are grouped by where they live:

- **Editor** features change how the editor looks or behaves.
- **Engine** features change what happens behind the scenes, such as
    how workflows run. They come from the engine you're connected to, so
    the list can differ between engines and engine versions.

To go back to the standard behavior for everything, click **Reset all**.
Every feature returns to its default, which is almost always off.

### When a toggle can't be changed

A toggle is greyed out when something outside your own settings is
choosing that feature's value. This happens when a project, a workspace,
or an environment variable sets it. A note under the toggle says which
one. Change the value there, or remove it, to control the feature from
this page again.

## What to expect from a beta feature

- **Beta features can change or disappear.** Each one is either made a
    standard part of Griptape Nodes or removed within a few months. When
    that happens, it drops off this page, and your saved choice for it
    has no effect.
- **Your workflows are safe either way.** A beta feature never changes
    what gets saved in a workflow. A workflow opens the same way whether a
    feature is on or off, so you can share workflows with people who have
    different features turned on.
- **Rough edges are expected.** If something behaves unexpectedly, turn
    the feature off to get the standard behavior back.

## Setting beta features without the editor

Your choices are saved in the `beta_features` section of your
`griptape_nodes_config.json` file, keyed by each feature's id:

```json
{
    "beta_features": {
        "parallel_branch_resolution": true
    }
}
```

Only `true` or `false` counts. Any other value, such as `"yes"`, is
ignored with a warning in the engine log, and the feature uses its
default. A mistake here never affects your other settings.

You can also turn a feature on for a single session with an
environment variable. Put the feature's id, in capitals, after
`GTN_CONFIG_BETA_FEATURES__`:

```bash
GTN_CONFIG_BETA_FEATURES__PARALLEL_BRANCH_RESOLUTION=true gtn
```

The feature names used above are examples. See
[Engine Configuration](../configuration.md) for how config files and
environment variables are loaded.
