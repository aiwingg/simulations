"""
Batch processing system for parallel conversation simulation
"""
import asyncio
import uuid
import json
import time
from typing import Dict, List, Any, Optional, Callable
from datetime import datetime
from dataclasses import dataclass, asdict
from enum import Enum
from src.config import Config
from src.openai_wrapper import OpenAIWrapper
from src.conversation_engine import ConversationEngine
from src.evaluator import ConversationEvaluator
from src.logging_utils import get_logger

class BatchStatus(Enum):
    """Batch processing status"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class BatchJob:
    """Batch job data structure"""
    batch_id: str
    scenarios: List[Dict[str, Any]]
    status: BatchStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_scenarios: int = 0
    completed_scenarios: int = 0
    failed_scenarios: int = 0
    progress_percentage: float = 0.0
    results: List[Dict[str, Any]] = None
    error_message: Optional[str] = None
    
    def __post_init__(self):
        if self.results is None:
            self.results = []
        if self.total_scenarios == 0:
            self.total_scenarios = len(self.scenarios)

class BatchProcessor:
    """Manages batch processing of conversation simulations"""
    
    def __init__(self, openai_api_key: str, concurrency: Optional[int] = None):
        self.concurrency = concurrency or Config.CONCURRENCY
        self.semaphore = asyncio.Semaphore(self.concurrency)
        self.logger = get_logger()
        
        # Initialize components
        self.openai_wrapper = OpenAIWrapper(openai_api_key)
        self.conversation_engine = ConversationEngine(self.openai_wrapper)
        self.evaluator = ConversationEvaluator(self.openai_wrapper)
        
        # Active batch jobs
        self.active_jobs: Dict[str, BatchJob] = {}
        
        self.logger.log_info(f"BatchProcessor initialized with concurrency: {self.concurrency}")
    
    def create_batch_job(self, scenarios: List[Dict[str, Any]]) -> str:
        """Create a new batch job"""
        batch_id = str(uuid.uuid4())
        
        batch_job = BatchJob(
            batch_id=batch_id,
            scenarios=scenarios,
            status=BatchStatus.PENDING,
            created_at=datetime.now(),
            total_scenarios=len(scenarios)
        )
        
        self.active_jobs[batch_id] = batch_job
        
        self.logger.log_info(f"Created batch job", extra_data={
            'batch_id': batch_id,
            'total_scenarios': len(scenarios)
        })
        
        return batch_id
    
    def get_batch_status(self, batch_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a batch job"""
        if batch_id not in self.active_jobs:
            return None
        
        job = self.active_jobs[batch_id]
        return {
            'batch_id': batch_id,
            'status': job.status.value,
            'progress': job.progress_percentage,
            'total_scenarios': job.total_scenarios,
            'completed_scenarios': job.completed_scenarios,
            'failed_scenarios': job.failed_scenarios,
            'created_at': job.created_at.isoformat(),
            'started_at': job.started_at.isoformat() if job.started_at else None,
            'completed_at': job.completed_at.isoformat() if job.completed_at else None,
            'error_message': job.error_message
        }
    
    def get_batch_results(self, batch_id: str) -> Optional[List[Dict[str, Any]]]:
        """Get results of a completed batch job"""
        if batch_id not in self.active_jobs:
            return None
        
        job = self.active_jobs[batch_id]
        return job.results
    
    async def run_batch(self, batch_id: str, progress_callback: Optional[Callable] = None) -> Dict[str, Any]:
        """Run a batch job with parallel processing"""
        
        if batch_id not in self.active_jobs:
            raise ValueError(f"Batch job {batch_id} not found")
        
        job = self.active_jobs[batch_id]
        
        if job.status != BatchStatus.PENDING:
            raise ValueError(f"Batch job {batch_id} is not in pending status")
        
        # Update job status
        job.status = BatchStatus.RUNNING
        job.started_at = datetime.now()
        
        self.logger.log_info(f"Starting batch processing", extra_data={
            'batch_id': batch_id,
            'total_scenarios': job.total_scenarios,
            'concurrency': self.concurrency
        })
        
        try:
            # Create tasks for all scenarios
            tasks = []
            for i, scenario in enumerate(job.scenarios):
                task = self._process_single_scenario(
                    scenario=scenario,
                    scenario_index=i,
                    batch_id=batch_id,
                    progress_callback=progress_callback
                )
                tasks.append(task)
            
            # Run all tasks concurrently
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            successful_results = []
            failed_count = 0
            
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    self.logger.log_error(f"Scenario {i} failed", exception=result, extra_data={'batch_id': batch_id})
                    failed_count += 1
                    
                    # Add failed result
                    failed_result = {
                        'scenario_index': i,
                        'scenario': job.scenarios[i].get('name', f'scenario_{i}'),
                        'status': 'failed',
                        'error': str(result),
                        'session_id': None,
                        'score': 1,
                        'comment': f"Ошибка обработки: {str(result)}"
                    }
                    successful_results.append(failed_result)
                else:
                    successful_results.append(result)
            
            # Update job with results
            job.results = successful_results
            job.completed_scenarios = len(successful_results)
            job.failed_scenarios = failed_count
            job.progress_percentage = 100.0
            job.status = BatchStatus.COMPLETED
            job.completed_at = datetime.now()
            
            duration = (job.completed_at - job.started_at).total_seconds()
            
            self.logger.log_info(f"Batch processing completed", extra_data={
                'batch_id': batch_id,
                'total_scenarios': job.total_scenarios,
                'successful': job.completed_scenarios - failed_count,
                'failed': failed_count,
                'duration_seconds': duration
            })
            
            return {
                'batch_id': batch_id,
                'status': 'completed',
                'total_scenarios': job.total_scenarios,
                'successful_scenarios': job.completed_scenarios - failed_count,
                'failed_scenarios': failed_count,
                'duration_seconds': duration,
                'results': successful_results
            }
            
        except Exception as e:
            # Update job with error
            job.status = BatchStatus.FAILED
            job.error_message = str(e)
            job.completed_at = datetime.now()
            
            self.logger.log_error(f"Batch processing failed", exception=e, extra_data={'batch_id': batch_id})
            
            raise e
    
    async def _process_single_scenario(self, scenario: Dict[str, Any], scenario_index: int, 
                                     batch_id: str, progress_callback: Optional[Callable] = None) -> Dict[str, Any]:
        """Process a single scenario with conversation and evaluation"""
        
        async with self.semaphore:  # Limit concurrency
            try:
                scenario_name = scenario.get('name', f'scenario_{scenario_index}')
                
                self.logger.log_info(f"Processing scenario {scenario_index}: {scenario_name}", extra_data={'batch_id': batch_id})
                
                # Run conversation
                conversation_result = await self.conversation_engine.run_conversation(scenario)
                
                # Evaluate conversation if successful
                if conversation_result.get('status') == 'completed':
                    evaluation_result = await self.evaluator.evaluate_conversation(conversation_result)
                    
                    # Combine results
                    combined_result = {
                        'scenario_index': scenario_index,
                        'scenario': scenario_name,
                        'session_id': conversation_result.get('session_id'),
                        'status': 'completed',
                        'total_turns': conversation_result.get('total_turns', 0),
                        'duration_seconds': conversation_result.get('duration_seconds', 0),
                        'score': evaluation_result.get('score', 1),
                        'comment': evaluation_result.get('comment', ''),
                        'evaluation_status': evaluation_result.get('evaluation_status', 'unknown'),
                        'start_time': conversation_result.get('start_time'),
                        'end_time': conversation_result.get('end_time')
                    }
                else:
                    # Conversation failed
                    combined_result = {
                        'scenario_index': scenario_index,
                        'scenario': scenario_name,
                        'session_id': conversation_result.get('session_id'),
                        'status': 'failed',
                        'error': conversation_result.get('error', 'Unknown error'),
                        'total_turns': conversation_result.get('total_turns', 0),
                        'duration_seconds': conversation_result.get('duration_seconds', 0),
                        'score': 1,
                        'comment': f"Разговор не завершен: {conversation_result.get('error', 'неизвестная ошибка')}"
                    }
                
                # Update progress
                if progress_callback:
                    await progress_callback(batch_id, scenario_index + 1)
                
                self.logger.log_info(f"Completed scenario {scenario_index}: {scenario_name}", extra_data={
                    'batch_id': batch_id,
                    'score': combined_result.get('score'),
                    'status': combined_result.get('status')
                })
                
                return combined_result
                
            except Exception as e:
                self.logger.log_error(f"Failed to process scenario {scenario_index}", exception=e, extra_data={'batch_id': batch_id})
                raise e
    
    async def _update_progress(self, batch_id: str, completed_count: int):
        """Update batch job progress"""
        if batch_id in self.active_jobs:
            job = self.active_jobs[batch_id]
            job.completed_scenarios = completed_count
            job.progress_percentage = (completed_count / job.total_scenarios) * 100.0
    
    def cancel_batch(self, batch_id: str) -> bool:
        """Cancel a running batch job"""
        if batch_id not in self.active_jobs:
            return False
        
        job = self.active_jobs[batch_id]
        if job.status == BatchStatus.RUNNING:
            job.status = BatchStatus.CANCELLED
            job.completed_at = datetime.now()
            self.logger.log_info(f"Cancelled batch job", extra_data={'batch_id': batch_id})
            return True
        
        return False
    
    def cleanup_completed_jobs(self, max_age_hours: int = 24):
        """Clean up old completed jobs"""
        current_time = datetime.now()
        jobs_to_remove = []
        
        for batch_id, job in self.active_jobs.items():
            if job.status in [BatchStatus.COMPLETED, BatchStatus.FAILED, BatchStatus.CANCELLED]:
                if job.completed_at and (current_time - job.completed_at).total_seconds() > max_age_hours * 3600:
                    jobs_to_remove.append(batch_id)
        
        for batch_id in jobs_to_remove:
            del self.active_jobs[batch_id]
            self.logger.log_info(f"Cleaned up old batch job", extra_data={'batch_id': batch_id})
        
        return len(jobs_to_remove)

