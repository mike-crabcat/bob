

Currently cyborg, during entity extraction will invent claim types on the fly. The majority of these arent useful.  Claim type should be a table and provide FK for claims and be injected for selection into the extraction llm prompt.  For each claim type we should have a claim type as a snake-key, a short description of purpose and an example.  Over time many duplciate claims may be produced and a dedicated process will de-dupe them and mark claims as redundant/disproven/obsolete etc.

Here are some ideas of claims for our entity types:

Contacts:
 - Alias
 - Physical appearance 
 - Child
 - Wife
 - Parent
 - Grand Parent
 - Grand child
 - Home address
 - Workplace
 - Job
 - Food preference
 - Drink preference
 - Interest
 - Personality

WorkspaceArtifact:
 - Path
 - Source Group
 - File Type
 - Description

Groups:
 - Alias 
 - Vibe = how people act in the group, how you are treated
 - Member

Event:
 - Start Time
 - Location
 - Purpose
 - Name
 
Location:
 - Type = venue, house, place,city etc
 - Parent location


Trip:
 - Start date
 - End date
 - Member
 - Stops : TripStop
 
Transport:
 - Type = plane, car, train
 - Departure Time
 - Duration
 
 
 TripStop:
 - Transport to : Transport
 - Transport from : Transport
 - Stay: Location
 - Arrival date/time
 - Departure date/time


What gaps can you see in our claim types and entities?
