- The editor's "Enable beta features" switch now turns engine and library beta features off too,
  not only editor ones. It is saved as `beta_features.enabled`. Each feature keeps its own setting
  and gets it back when the switch is turned on again. `GTN_CONFIG_BETA_FEATURES__ENABLED=false`
  turns every beta feature off for one session.
  [#5710](https://github.com/griptape-ai/griptape-nodes-engine/issues/5710)
