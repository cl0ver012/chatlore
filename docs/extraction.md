# The knowledge graph

```bash
chatlore process                 # chunk and embed first
chatlore extract --limit 50      # try the model on a few chunks
chatlore extract                 # read the rest; safe to repeat and to interrupt
chatlore topics                  # what the conversations are about
chatlore entity "Postgres"       # one entity, its links, and where it came up
chatlore facts backups           # what was decided or found, and where
```

`chatlore extract` builds a graph from your conversations with the configured
language model (see [models.md](models.md)), in four steps:

1. **Entities, relationships, and facts.** Each chunk is read, and the model
   names the entities it mentions, how they relate, and the facts it states
   about them. Every chunk links to the entities it mentions, and every fact to
   the messages that said it, so anything in the graph leads back to where it
   came from.
2. **Summaries.** An entity mentioned in several places gets one short summary.
3. **Duplicates.** Names that may mean the same thing, such as "AWS" and "Amazon
   Web Services", are checked by the model and linked when they do.
4. **Topics.** Closely related entities are grouped, and the model writes a
   short report on each group.

Everything the model answers is cached, so each step only asks about what is new.

## What it costs

Chunks go four to a request and eight requests run at once, and the default
model answers without reasoning first, which makes it roughly ten times faster
and cheaper than a reasoning model for this work (see [models.md](models.md)).
Measured on a real Claude export with the default settings:

| Run | What it did | Time | Cost |
|---|---|---|---|
| First run on 124 conversations, 2,043 chunks | Read 1,999 distinct chunk texts, summarised 982 entities, judged 1,186 possible duplicates, wrote 214 topic reports | 24 min | about 10 US cents |
| Second run | Read the 17 chunks whose answers could not be used the first time, and updated what they changed | 2 min 20 s | under 1 US cent |

The result was 3,214 entities, 3,197 relationships, 200 pairs of names for the
same thing, and 212 topics.

The summary after each run shows the tokens used, and `--limit` reads only that
many new chunks, so check the output and the cost on a small slice first.
`--batch-size` and `--workers` change how many chunks go into one request and
how many requests run at the same time.

## Only new text is read

Answers are stored in `~/.chatlore/cache/extractions.db`: extractions by the
model, the prompt version, and the chunk text; summaries by the entity's name and
set of descriptions; duplicate verdicts by the pair of names; topic reports by
the set of entities in the topic. A second run asks nothing it has asked before,
an interrupted run resumes where it stopped, and an answer that could not be
used, such as an empty one, is asked again next time. Switching models asks
everything again with the new model.

The graph is rebuilt from those stored answers at the end of every run. Text
removed by a re-import takes its entities with it, and after
`chatlore index --rebuild` and `chatlore process`, `chatlore extract` restores
the whole graph, summaries, duplicates, and topics included, without calling the
model.

## What an entity is

The model picks a type for each entity. It is asked to prefer person,
organization, project, tool, concept, place, and event, and may use another
single word when none fits; on a real export 98% of entities used the seven.
Types that clearly mean one of them, such as company, city, or language, are
counted as that one. The model is also asked to skip labels that only make sense
inside their passage, such as "Solution A" or "Step 3".

Mentions are one entity when their names match ignoring case, spacing, hyphens
and underscores between words, trailing punctuation, and a plural "s" on the
last word: "Drive D:" and "drive d", or "Challenge fees" and "Challenge fee".
Punctuation inside a name counts, so "C++", "C#", and "C" stay apart.

## Facts

A fact is something a passage establishes that is worth remembering later: a
decision, a preference, a plan, a setting or number, a finding, or a problem and
its fix. General knowledge and options that were only considered are left out.
Each fact is one self-contained sentence about one entity, such as "The shop
backend moved from MySQL to PostgreSQL to get JSON columns.", and may involve a
second entity.

In the graph a fact links to its entity (`SUBJECT`), to the second entity if any
(`OBJECT`), and with `ASSERTED_IN` to every message that said it. The same
sentence said in several places, ignoring case, spacing, and trailing
punctuation, is one fact with all of those messages, and it is gone once none
of them is left. The latest 20 are kept on the fact with their conversation,
title, and date, for showing where it was said.

`chatlore facts` lists facts, newest first, and `chatlore facts <words>` finds
those that mention the words; each shows the conversation and date it was last
said in. `chatlore entity <name>` shows the facts about an entity, the web
interface shows them in the entity drawer with a link to the message, the MCP
server has a `facts` tool, and chat gets the facts about the entities a
question names.

On the demo library of 32 conversations, 86 chunks gave 118 facts.

Facts came with ChatLore 0.3. A library extracted before that keeps its
entities, relationships, and topics, and the next `chatlore extract` reads every
chunk once more to find its facts, at about the cost of the first run;
`--limit` spreads that over several runs.

## Summaries

Every distinct description of an entity is kept. An entity described once uses
that description as its summary. One described more often gets a summary of at
most 50 words from the model, written from up to 20 of its descriptions, and it
is written again only when a new description turns up.

## Duplicates

Different names for one thing are found in two passes. A cheap first pass
proposes pairs: one name starting with the other or spelled almost alike, one
being the other with words added, or one being the other's initials. The model
then judges each pair from both summaries; different versions, different
products of one company, and a part and its whole do not count as the same.
Confirmed pairs are linked with `SAME_AS`, and both entities stay.

On a real export, 200 of 1,198 proposed pairs were confirmed, such as "AWS" and
"Amazon Web Services", "DRL" and "Deep Reinforcement Learning", or "RTX 4060" and
"NVIDIA RTX 4060". In a sample of 15, one was wrong ("support" and "support
level") and one debatable (a tool and its viewer app). Name similarity alone
would have been wrong most of the time: it also proposes pairs like "Llama" and
"Ollama", which the model rejects.

## Topics

Entities weighted by their relationships form a graph, and Leiden community
detection splits it into groups linked more densely inside than to each other.
Entities linked as duplicates count as one, entities with no relationships are
left out, and a group over 40 entities is split again on its own, so one
well-connected entity cannot pull half the graph into one topic. Each group of
at least three entities becomes a topic with a report: a title, a summary of at
most 80 words, and up to five findings. A topic whose entities are unchanged
keeps its report; new text can move entities between topics, and those topics
get new reports.

A topic can stay over 40 entities when Leiden finds nothing to split. On a real
export the largest topic, 84 entities, was a star: one entity took part in 96% of
its relationships and 75 members linked only to it. Splitting it would have left
single entities, too small to be topics. The next largest topics had 40, 38, and
37 entities.

`chatlore topics` lists topics, largest first, and `chatlore topics <words>`
finds those whose report mentions the words. `chatlore entity <name>` shows an
entity's summary, its other names, its topic, its strongest relationships, the
facts about it, and the conversations that mention it.

## Search

`chatlore search --hybrid` also finds messages through the graph: a message whose
chunks mention an entity matching the query by name or summary joins the ranking,
and such hits say `entities` among the ways they matched.
