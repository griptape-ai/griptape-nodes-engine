- A workflow embedded in a PNG loads in a later session even when a node holds a value whose class
  a node library defines, such as the library's own enum or artifact. PNG files exported by earlier
  versions with such values load too, instead of failing.
