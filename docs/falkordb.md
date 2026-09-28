# Keeping the graph in FalkorDB

```bash
pip install 'chatlore[falkordb]'
docker run -d -p 6379:6379 --name falkordb falkordb/falkordb
export CHATLORE_STORE=falkordb
chatlore index           # copy the library into FalkorDB
chatlore process         # chunks and embeddings
chatlore extract         # entities and topics, from the cached answers where possible
```

By default ChatLore keeps its graph in a SQLite file in the library folder,
which needs nothing else. [FalkorDB](https://www.falkordb.com) is a graph
database that runs as a server. Use it when you want to query the graph with
Cypher, look at it with FalkorDB's browser, or share one graph between several
machines on a network. Every command, the web interface, and the MCP server
work the same on both.

## Settings

| Variable | Meaning |
|---|---|
| `CHATLORE_STORE` | `sqlite` (default) or `falkordb`. |
| `CHATLORE_FALKORDB_URL` | The server, `redis://localhost:6379` by default. Add a password as `redis://:password@host:6379`. |
| `CHATLORE_FALKORDB_GRAPH` | The graph's name, `chatlore` by default. Give each library its own. |

`chatlore doctor` shows which store is in use.

## Moving a library into FalkorDB

The conversations stay in the library folder either way, and the caches of
embeddings and model answers too, so the graph can be rebuilt in FalkorDB
without computing anything again:

1. Set the variables above.
2. `chatlore index` copies every conversation into the graph.
3. `chatlore process` makes the chunks and takes their embeddings from the cache.
4. `chatlore extract` rebuilds entities, relationships, and topics from the
   cached model answers. It needs a model key, but only asks the model about
   text it has not read yet.

Going back to SQLite is the same with `CHATLORE_STORE` unset. An archive from
`chatlore export` imports into either store. `chatlore index --rebuild` deletes
the graph and builds it again from the library.

## What is in the graph

Every node has the label `Node`, with its id in `_id`, its ChatLore label
(`Conversation`, `Message`, `Chunk`, `Entity`, `Topic`) in `_label`, and all
of its properties as JSON in `_props`. Simple values are copied into `p_`
properties as well, so they can be queried: an entity's name is `p_name`, a
topic's title is `p_title`. Edges have their ChatLore type, such as `MENTIONS`,
`RELATED_TO`, or `IN_TOPIC`, with their properties as JSON in `_props`.

```cypher
MATCH (e:Node {_label: 'Entity'})-[r:RELATED_TO]-(o:Node)
WHERE e.p_name = 'Tidewater'
RETURN o.p_name, r._props
```

Everything ChatLore adds for search, the `_ft_` text copies and the
`_embedding` vectors, is indexed in FalkorDB. Treat the graph as ChatLore's:
changes made to it directly are lost on the next `chatlore index --rebuild`.

## Differences from SQLite

- FalkorDB has no transactions spanning several queries. Each write is atomic,
  but an import interrupted halfway leaves the conversations written so far;
  running it again finishes the job, as with SQLite.
- Word search matches the same words as SQLite, without stemming or stop words,
  and ignores accents. Its scores are FalkorDB's own, so results with equal
  matches can come back in a different order.
- The server has to be running. Commands fail with a connection error when it
  is not.
- Every page and command asks the server many small questions, so keep it
  close: on the same machine or network. Against a FalkorDB Cloud server a
  continent away, 0.16 seconds per query, loading the demo took a minute and a
  half, a search 8 to 10 seconds, and the graph view almost two minutes.
- Each conversation takes a few round trips to the server, so importing is
  slower. On a made-up library of 12,000 notes, importing took 31 seconds
  against 8 with SQLite, chunking 26 seconds against 145, and a search the same
  time on both.
