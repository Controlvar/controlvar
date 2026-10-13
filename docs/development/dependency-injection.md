# Route Dependencies

How an API route gets its database session, its caller and its permission
checks. They are FastAPI dependencies, in `backend/app/api/deps.py`.

## The dependencies

| Dependency | What the route gets |
|---|---|
| `get_db` | a database session, closed when the request ends |
| `get_current_active_user` | the signed-in caller; an inactive account is refused with `400` |
| `get_current_superuser` | the caller, who must be a superuser; anyone else is refused with `403` |
| `get_api_key` | the user an `X-API-Key` header belongs to; a missing or invalid key is refused with `401` |

```python
@router.get("/")
async def read_items(
    current_user: User = Depends(deps.get_current_active_user),
):
    return {"message": f"Hello, {current_user.username}"}
```

## Permission checks

- **Experiments.** The route loads the experiment, then calls
  `deps.get_experiment_read_access(experiment, current_user)` to read it, or
  `deps.get_experiment_change_access(experiment, current_user)` to change it.
  Both are synchronous and refuse with `403`, and neither looks at who created the
  experiment.
- **Feature flags.** Access is by role, not by owner. A route that changes a flag
  calls `can_act_on_feature_flag(user, owner_id, action)` from
  `backend/app/core/permissions.py`, and creating one goes through the async
  `deps.can_create_feature_flag`. Build a new route on those two, not on the other
  flag helpers in `deps.py`.
- **Reports.** The report dependencies are async: await them.

```python
@router.get("/{experiment_id}/results")
async def get_experiment_results(
    experiment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    deps.get_experiment_read_access(experiment, current_user)
    ...
```
