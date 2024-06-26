import itertools
import logging
from datetime import datetime
from operator import ne
from typing import List

import cohere
import openai
import tiktoken

from koma.domain.models.note import Note
from koma.infra.fetchers.apple import AppleNotesFetcher
from koma.infra.renderers.markdown import MarkDown

from ..domain.enum import EmbedModel, IndexState, MemoryType
from ..domain.manager import (
    MemoryManager,
    MemorySyncLogManager,
    NeuronIndexLogManager,
    NeuronManager,
)
from ..domain.models import Memory, MemorySyncLog, Neuron, NeuronIndexLog, Position
from ..dto.memory import MemoryDTO, NeuronDTO

logger = logging.getLogger(__name__)
openai_client = openai.OpenAI()
cohere_client = cohere.Client()

class MemorySerivce:
    def sync_modified_memories(self) -> List[Memory]:
        markdown = MarkDown()
        fetcher = AppleNotesFetcher(markdown)
        fetcher.start()

        notes = fetcher.notes
        uuids = [note.uuid for note in notes]

        logs = MemorySyncLogManager().list_by_biz_ids(uuids)
        last_modified_map_by_biz_id = {log.biz_id: log.biz_modified_at for log in logs}

        to_create_notes = [
            note 
            for note in notes 
            if note.uuid not in last_modified_map_by_biz_id.keys()
        ]

        to_update_notes = [
            note 
            for note in notes 
            if note.uuid in last_modified_map_by_biz_id.keys() and note.modified_at > last_modified_map_by_biz_id.get(note.uuid)
        ]
        
        to_update_biz_ids = list(map(lambda note: note.uuid, to_update_notes))
        to_update_memories = MemoryManager().list_by_biz_ids(to_update_biz_ids)


        created_memories = [
            Memory(memory_type = MemoryType.NOTE, data = note, biz_id = note.uuid)
            for note in to_create_notes
        ]
        
        to_update_notes_map_by_biz_id = {note.uuid: note for note in to_update_notes}
        for memory in to_update_memories:
            note =  to_update_notes_map_by_biz_id.get(memory.biz_id)
            if note is None:
                continue
            
            memory.data = note

        MemoryManager().bulk_create(created_memories)
        MemoryManager().bulk_update(to_update_memories, fields=["data"], batch_size=20)

        memories = created_memories + to_update_memories

        sync_logs = [
            MemorySyncLog(
                biz_id = memory.biz_id,
                biz_modified_at = memory.data.modified_at
            )
            for memory in memories
        ]

        MemorySyncLogManager().bulk_create(sync_logs)

        return memories


class NeuronService:
    def index_memories(self,  memories: List[Memory]):

        indexLogs = [
            NeuronIndexLog(memory_id = memory.memory_id)
            for memory in memories
        ]
        NeuronIndexLogManager().bulk_create(indexLogs)

        memory_ids = [memory.memory_id for memory in memories]
        NeuronManager().delete_by_memory_ids(memory_ids)

        neurons_mapper = map(generate_neurons, memories)
        neurons = list(itertools.chain.from_iterable(neurons_mapper))

        NeuronManager().bulk_create(neurons)
        
        for indexLog in indexLogs:
            indexLog.state = IndexState.INDEXED
            indexLog.indexed_at = datetime.now().astimezone()

        NeuronIndexLogManager().bulk_update(indexLogs, fields=["state", "indexed_at"], batch_size=20)

        return

    def query_similar(self, query: str, topk: int) -> List[Neuron]:
        result = openai_client.embeddings.create(input = query, model = "text-embedding-3-small")
        embedding = result.data[0].embedding

        neurons = NeuronManager().list_within_distance_on_embedding(embedding = embedding, distance = 0.80)
        reranked = self.rerank_neurons(neurons=neurons,query=query,topk=topk)
        return reranked

    @staticmethod
    def rerank_neurons(neurons: List[Neuron], query: str, topk: int) -> List[Neuron]:
        
        neuron_map_by_idx = {idx: neuron for idx, neuron in enumerate(neurons)}
        documents = [neuron.content for neuron in neurons]
        rerank_result = cohere_client.rerank(query=query, documents=documents, model="rerank-multilingual-v2.0", top_n=topk).results
    
        rerank_result_idx = [item.index for item in rerank_result]
        return [
            neuron_map_by_idx[idx]
            for idx in rerank_result_idx
        ]


class MemoryBizService:
    
    memory_service = MemorySerivce()
    neuron_service = NeuronService()

    def list_all(self) -> List[MemoryDTO]:
        memories = MemoryManager().list_all()
        dto = MemoryDTO.from_model_list(memories)
        return dto
    
    def sync_memories(self):
        memories = self.memory_service.sync_modified_memories()

        if len(memories) == 0:
            return
        
        self.neuron_service.index_memories(memories)
        return
    

def generate_neurons(memory: Memory) -> List[Neuron]:
    note: Note = memory.data
    paragraph_list = note.content.paragraph_list
    if len(paragraph_list) == 0:
        return []
    
    # check if represent is not blank
    indexable_paragraph = [
        {"idx":idx, "content": p.rendered_result} 
        for idx, p in enumerate(paragraph_list) 
        if p.rendered_result is not None and not p.rendered_result.isspace()
    ]

    if len(indexable_paragraph) == 0:
        return []

    enc = tiktoken.get_encoding("cl100k_base")
    total_token = sum([len(enc.encode(c["content"])) for c in indexable_paragraph])

    if total_token >= 8191:
        logger.warning(f"Memory {memory.memory_id} has too many tokens {total_token}")
        return []
    
    content = [p["content"] for p in indexable_paragraph]
    result = openai_client.embeddings.create(input = content, model = "text-embedding-3-small")

    neurons = [
        Neuron(
            embedding = data.embedding,
            content = indexable_paragraph[data.index]["content"],
            memory_id = memory.memory_id,
            position = Position(paragraph = indexable_paragraph[data.index]["idx"]),
            embed_model = EmbedModel.OPENAI_TEXT_EMBEDDING_3_SMALL
        )
        for data in result.data
    ]

    return neurons


class NeuronBizSerivces:
    def search_neurons(self, query: str, topk: int) -> List[NeuronDTO]:
        if query is None or query == '':
            return []
        
        neurons = NeuronService().query_similar(query, topk)

        return NeuronDTO.from_model_list(neurons)
    
    def search_neurons_as_text(self, query, topk: int) -> str:

        if query is None or query == '':
            return ""

        neurons = NeuronService().query_similar(query, topk)

        return "---\n".join([str(neuron) for neuron in neurons])