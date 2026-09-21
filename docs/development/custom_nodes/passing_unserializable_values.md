# Passing Values That Cannot Be Serialized

Some values cannot be turned into data. A diffusers pipeline, a latent tensor, an
open file handle, a live driver — there is no JSON for them. If your library runs
isolated in a worker subprocess (see
[Node Isolation with Workers](node_isolation_with_workers.md)), parameter values
travel between the orchestrator and your worker as JSON, so passing one of these
from one of your nodes to the next needs help.

The short version: **mark the producing output `serializable=False` and assign the
object to it. The consumer declares nothing and reads the object normally.**

```python
class LoadPipeline(ControlNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.add_parameter(
            Parameter(
                name="pipeline",
                output_type="Pipeline",
                tooltip="The loaded pipeline",
                serializable=False,
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["pipeline"] = load_pipeline(...)


class Generate(ControlNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # No declaration needed on the consuming side.
        self.add_parameter(Parameter(name="pipeline", input_types=["Pipeline"], tooltip="Pipeline to run"))

    def process(self) -> None:
        pipeline = self.get_parameter_value("pipeline")  # the real object
        ...
```

## What actually happens

The object stays in the process that built it. When the value is about to cross a
process boundary the engine substitutes an opaque **key** — a short string — and
sends that instead. When your consuming node reads its parameter, the key becomes
the object again.

Three consequences worth internalising:

- **Your node's own dicts always hold the real thing.** Assign a pipeline to
    `parameter_output_values["pipeline"]` and read it straight back and you get the
    pipeline, not a key. Nothing is substituted on a write.
- **A graph that never leaves the process never does any of this.** If your library
    runs in Shared mode, values pass by reference exactly as they always have.
- **Only the producer declares.** The key travels down connections to consumers
    that declare nothing at all, which is why the consuming parameter above is an
    ordinary `Parameter`.

## `serializable=False` means two things now

It has always meant "keep this out of saved workflow files" — the right choice for
drivers, file handles and large transient buffers. It now also means "hold this in
the process that made it, and send a key across a process boundary."

Those are the same fact about an object: something that cannot be written to a file
cannot be written to a socket either. But the flag is not a switch you throw to get
holding behaviour, because **plain data on a declared parameter is still sent as
data**:

| Value on a `serializable=False` output | What crosses                 | Why                                                                        |
| -------------------------------------- | ---------------------------- | -------------------------------------------------------------------------- |
| a pipeline, a tensor, a driver         | a key; the object stays here | there is no data form of it                                                |
| an API key string                      | the string                   | it travels perfectly well, and a key would be unresolvable on the far side |
| a `dict` of numbers, a list of strings | the value                    | already data                                                               |
| an `ImageUrlArtifact`                  | a key; the object stays here | see below                                                                  |

That last row surprises people. An artifact a library defines itself unstructures
into a dict of its fields and loses its payload on the way, so the engine will not
gamble on a round trip: if you declared the parameter, your object is held. If you
want an artifact to travel as data — which is usually what you want for anything
with a URL in it — **do not declare the parameter**. Undeclared artifacts serialize
and rehydrate exactly as they always have.

## Reusing an expensive resource across runs

The above covers values flowing between nodes. A *resource* you want to build once
and reuse — a pipeline whose load takes 30 seconds — needs a key you can derive
again, so it has its own small API on the node:

```python
def process(self) -> None:
    key = self.local_objects.key_for(self._config_hash())
    pipeline = self.local_objects.get(key)
    if pipeline is None:
        pipeline = build_pipeline(...)
        self.local_objects.put(pipeline, key=self._config_hash(), on_drop=release_vram)
    ...
```

| Call                                             | Does                                                                         |
| ------------------------------------------------ | ---------------------------------------------------------------------------- |
| `local_objects.put(value, *, key, on_drop=None)` | holds `value` under your key, returns the full namespaced key                |
| `local_objects.get(key)`                         | the object, or `None` if this process is not holding it                      |
| `local_objects.key_for(suffix)`                  | the full key for a suffix you chose, without putting anything                |
| `local_objects.drop(key)`                        | releases one object; returns whether it was holding it                       |
| `local_objects.drop_all()`                       | releases everything your library holds here; what a "clear cache" node calls |

`put` under a key you already used releases what was there first, unless it is the
same object — so rebuilding under an unchanged hash does not strand the old one.

## Freeing what the object holds

Dropping the last Python reference does not free VRAM. Pass `on_local_object_drop`
on the parameter (or `on_drop` to `put`) and the engine calls it when the object is
released:

```python
Parameter(
    name="pipeline",
    output_type="Pipeline",
    tooltip="The loaded pipeline",
    serializable=False,
    allowed_modes={ParameterMode.OUTPUT},
    on_local_object_drop=lambda pipeline: pipeline.to("cpu"),
)
```

The hook belongs to the cache, so it runs when the cache lets an object go:

- **your node runs again** and the cache takes a new object for that parameter —
    the one it was holding is released;
- **your node is deleted** and nothing else still refers to the object;
- **your library is unloaded**, or every library is reloaded;
- **the workflow is closed or cleared.**

It runs once per object, even when one object sits on two outputs.

What it does *not* cover is an object that never reached the cache. If your library
runs in Shared mode there is no process boundary, so nothing is ever cached and the
value simply passes by reference the way it always has — there is nothing for the
cache to release, and freeing it is yours to do as it was before. The same is true
of an object you overwrite mid-run: only what the parameter holds when the node
finishes goes in. Two other cases where the hook will not have run: updating a
single library or switching its git ref does not restart its worker today, so
objects that worker holds survive into the new code.

## What you cannot do

**A list or dictionary parameter cannot hold a value.** `ParameterList` and
`ParameterDictionary` build their value from their children, so there is no single
object to hold and nowhere to put a release hook. Declaring `serializable=False` on
one still does what it always did — keeps the list out of saved workflows — but it
adds no caching.

Output the whole batch on an ordinary `Parameter` marked `serializable=False` — a
list of tensors is one object as far as holding is concerned, and that works. A
`ParameterList` *consuming* held values is fine: each row carries its own key, and
`get_parameter_value` on the container gives you the objects. Prefer that over
`get_parameter_list_value` for held values, because the latter flattens anything
iterable and takes a list of tensors apart into their rows.

**An object cannot leave the process that built it.** The cache belongs to the
worker, not to your library: one worker can host several libraries and they share
it, so a co-hosted library handed a key resolves it fine. What does not work is
reading a key from a *different* process — another worker, or the orchestrator:

> Attempted to read the value for parameter 'pipeline' on node 'Generate'. Failed
> due to: it is held in another process, and an object cannot leave the process
> that built it. Read it from a node that runs in the same place as the one that
> made it, or have that node output a saved file instead.

Two libraries share a worker only when neither declares its own execution venv, and
that is not something a graph author can see. So if you ship a library that hands
objects to a *different* library's nodes, do not rely on co-hosting — write a file
and pass its path or URL. Within your own library you are always in one worker.

Keys you choose through `put` are namespaced by your library inside the worker, so a
co-tenant using the same string for its own cache cannot collide with yours.

**An input is never cached.** Caching one would mint a key the far side has nothing
to resolve against: the object is in the sending process while the node runs
elsewhere. Only outputs go in the cache, so an object handed to a worker-bound node
as an input value gets whatever the transport makes of it, exactly as before. If you
need it over there, have the node that produces it run in the same library, or write
a file.

**Nothing is held across a reload.** Keys refer to memory in a running process.

## Errors you may see, and what they mean

| Message contains                                                                                             | Means                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `it is no longer available, which happens after the workflow is reloaded or the node that made it is re-run` | the key was minted in this process and what it named has since been released. Re-run the producer; this is working as intended, not a lost object                             |
| `held in another process`                                                                                    | you are reading it from somewhere other than the process that built it — most often from validation or a value hook, which run in the orchestrator rather than in your worker |
| `nothing is connected to it`                                                                                 | the input is unwired                                                                                                                                                          |

That second one is a feature. Keys are unique per assignment, so a consumer
holding one from a previous run finds it dangling rather than silently resolving to
a *newer* object it was never given.

## Saving and metadata

A held value is never written into a saved workflow, and a node carrying one comes
back **UNRESOLVED** so its producer re-runs and replaces it. Workflow metadata and
image sidecars report the parameter as omitted rather than recording a key. You do
not have to do anything for this; it is the same declaration doing the work.

## Checklist

- Producing output declared `serializable=False`, with `on_local_object_drop` if
    releasing it takes more than dropping a reference.
- Consuming parameter declares nothing.
- Producer and consumer run in the same worker, which is automatic within one library.
- Anything with a URL in it (`ImageUrlArtifact` and friends) is left undeclared so
    it travels as data.
- If your nodes can run in Shared mode, do not rely on the release hook: nothing is
    cached there, so nothing is released.
- Batches go on an ordinary parameter, not a `ParameterList` output.
