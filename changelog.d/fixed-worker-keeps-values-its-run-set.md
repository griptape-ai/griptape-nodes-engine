- A node whose library runs in a worker now keeps a value it sets on itself while running, such as a
  seed it rolls. Before, turning off the node's randomize setting after a run went back to the seed
  from before that run, and saving the workflow wrote the old seed.
