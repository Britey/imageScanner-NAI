--------------------------- MODULE UniqueTags ---------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Images, Tags, Kinds, Absent, NoTag, ModelType, SuppressConfigured
ASSUME /\ IsFiniteSet(Images) /\ Images # {}
       /\ IsFiniteSet(Tags) /\ Tags # {}
       /\ IsFiniteSet(Kinds) /\ ModelType \in Kinds
       /\ Absent \notin Kinds /\ NoTag \notin Tags
       /\ SuppressConfigured \in BOOLEAN

VARIABLES rows, prompts, configured, todo, decided, accepted,
          active, candidates, count, visited, probeCount
vars == <<rows, prompts, configured, todo, decided, accepted,
          active, candidates, count, visited, probeCount>>
inputs == <<rows, prompts, configured>>

\* rows models the prompt_tags primary key (image_id, tag_norm).
\* Absent means no row. Non-model_type kinds obey the same visibility rule.
Names == {t \in Tags : \E i \in Images : rows[<<i, t>>] # Absent}
Occurrences(t) == {i \in Images : rows[<<i, t>>] # Absent}

\* The original JOIN + COUNT(DISTINCT), independently stated as a set.
Legacy == {t \in Tags : \E i \in Images :
    /\ rows[<<i, t>>] # Absent
    /\ i \in prompts
    /\ ~(rows[<<i, t>>] = ModelType /\ i \in configured)}

\* The EXISTS probe. SuppressConfigured=FALSE is an intentional bad mutant.
Visible(i, t) == /\ i \in prompts
                 /\ ~(SuppressConfigured /\ rows[<<i, t>>] = ModelType
                       /\ i \in configured)

Init == /\ rows \in [Images \X Tags -> Kinds \cup {Absent}]
        /\ prompts \in SUBSET Images
        /\ configured \in SUBSET prompts
        /\ todo = Names
        /\ decided = {} /\ accepted = {}
        /\ active = NoTag /\ candidates = {}
        /\ count = 0 /\ visited = {} /\ probeCount = 0

Start == /\ active = NoTag /\ todo # {}
         /\ \E t \in todo : /\ active' = t
                              /\ candidates' = Occurrences(t)
         /\ UNCHANGED <<inputs, todo, decided, accepted, count, visited, probeCount>>

Probe == /\ active # NoTag /\ candidates # {}
         /\ \E i \in candidates :
             /\ visited' = visited \cup {<<i, active>>}
             /\ probeCount' = probeCount + 1
             /\ IF Visible(i, active)
                   THEN /\ accepted' = accepted \cup {active}
                        /\ count' = count + 1
                        /\ decided' = decided \cup {active}
                        /\ todo' = todo \ {active}
                        /\ active' = NoTag /\ candidates' = {}
                   ELSE /\ candidates' = candidates \ {i}
                        /\ UNCHANGED <<todo, decided, accepted, active, count>>
         /\ UNCHANGED inputs

Reject == /\ active # NoTag /\ candidates = {}
          /\ decided' = decided \cup {active}
          /\ todo' = todo \ {active}
          /\ active' = NoTag
          /\ UNCHANGED <<inputs, candidates, accepted, count, visited, probeCount>>

Done == /\ todo = {} /\ UNCHANGED vars
Next == Start \/ Probe \/ Reject \/ Done
Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

TypeOK == /\ rows \in [Images \X Tags -> Kinds \cup {Absent}]
          /\ prompts \subseteq Images /\ configured \subseteq prompts
          /\ todo \subseteq Names /\ decided = Names \ todo
          /\ accepted \subseteq decided
          /\ active \in Tags \cup {NoTag}
          /\ candidates \subseteq Images /\ count \in Nat /\ probeCount \in Nat
          /\ visited \subseteq Images \X Tags
          /\ IF active = NoTag THEN candidates = {}
                               ELSE active \in todo /\ candidates \subseteq Occurrences(active)
CorrectDecisions == accepted = Legacy \cap decided
NoDoubleCount == count = Cardinality(accepted)
\* Hidden rows can be discarded; a visible witness must remain until accepted.
WitnessRetained == active = NoTag \/
    \A i \in Occurrences(active) \ candidates : ~Visible(i, active)
NoRepeatedProbe == probeCount = Cardinality(visited)
BoundedProbes == probeCount <=
    Cardinality({p \in Images \X Tags : rows[p] # Absent})
ResultAtCompletion == todo = {} => count = Cardinality(Legacy)
EventuallyComplete == <>(todo = {})
=============================================================================
