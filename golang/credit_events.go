package bursar

import (
	"context"
	"sync"
	"time"
)

// CreditEventType identifies a committed credit lifecycle event.
type CreditEventType string

const (
	CreditEventDeducted            CreditEventType = "credits.deducted"
	CreditEventDeductFailed        CreditEventType = "credits.deduct_failed"
	CreditEventAdded               CreditEventType = "credits.added"
	CreditEventRefunded            CreditEventType = "credits.refunded"
	CreditEventRefundFailed        CreditEventType = "credits.refund_failed"
	CreditEventExpired             CreditEventType = "credits.expired"
	CreditEventLowBalance          CreditEventType = "credits.low_balance"
	CreditEventPlanChanged         CreditEventType = "credits.plan_changed"
	CreditEventReserved            CreditEventType = "credits.reserved"
	CreditEventReservationReleased CreditEventType = "credits.reservation_released"
	CreditEventLeaseExpired        CreditEventType = "credits.lease_expired"
	CreditEventOverdraft           CreditEventType = "credits.overdraft"
	CreditEventCycleRenewed        CreditEventType = "credits.cycle_renewed"
	CreditEventRevoked             CreditEventType = "credits.revoked"
	CreditEventQuotaBlocked        CreditEventType = "credits.quota_blocked"
	CreditEventQuotaThreshold      CreditEventType = "credits.quota_threshold"
)

// CreditEvent is emitted only after a successfully committed store operation,
// except for explicit business-denial events such as credits.deduct_failed.
// Amounts remain Amount values and are never converted through float64.
type CreditEvent struct {
	Type      CreditEventType
	Timestamp time.Time
	UserID    string
	Data      CreditMetadata
}

// CreditEventHandler receives one event. A handler is isolated from SDK
// accounting: a panic in a handler is recovered and cannot roll back or alter
// a committed credit operation.
type CreditEventHandler func(context.Context, CreditEvent)

// CreditEventSink lets applications replace the in-process emitter with an
// adapter to their event bus while retaining the SDK's event contract.
type CreditEventSink interface {
	Emit(context.Context, CreditEvent)
}

// CreditEventEmitter is a goroutine-safe in-process event emitter. It calls
// handlers synchronously after taking a snapshot, so handlers can subscribe or
// unsubscribe without deadlocking the dispatcher.
type CreditEventEmitter struct {
	mu        sync.RWMutex
	listeners map[CreditEventType]map[uint64]CreditEventHandler
	nextID    uint64
}

// NewCreditEventEmitter creates an empty event emitter.
func NewCreditEventEmitter() *CreditEventEmitter {
	return &CreditEventEmitter{listeners: make(map[CreditEventType]map[uint64]CreditEventHandler)}
}

// On subscribes handler and returns an idempotent unsubscribe function.
func (e *CreditEventEmitter) On(eventType CreditEventType, handler CreditEventHandler) func() {
	if e == nil || handler == nil {
		return func() {}
	}
	e.mu.Lock()
	e.nextID++
	id := e.nextID
	if e.listeners[eventType] == nil {
		e.listeners[eventType] = make(map[uint64]CreditEventHandler)
	}
	e.listeners[eventType][id] = handler
	e.mu.Unlock()

	var once sync.Once
	return func() {
		once.Do(func() { e.off(eventType, id) })
	}
}

func (e *CreditEventEmitter) off(eventType CreditEventType, id uint64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	handlers := e.listeners[eventType]
	if handlers == nil {
		return
	}
	delete(handlers, id)
	if len(handlers) == 0 {
		delete(e.listeners, eventType)
	}
}

// Clear removes every listener for eventType.
func (e *CreditEventEmitter) Clear(eventType CreditEventType) {
	if e == nil {
		return
	}
	e.mu.Lock()
	delete(e.listeners, eventType)
	e.mu.Unlock()
}

// ClearAll removes all registered listeners.
func (e *CreditEventEmitter) ClearAll() {
	if e == nil {
		return
	}
	e.mu.Lock()
	e.listeners = make(map[CreditEventType]map[uint64]CreditEventHandler)
	e.mu.Unlock()
}

// Emit dispatches an event to a snapshot of handlers. Listener failures never
// escape because accounting must not be coupled to observability callbacks.
func (e *CreditEventEmitter) Emit(ctx context.Context, event CreditEvent) {
	if e == nil {
		return
	}
	if event.Timestamp.IsZero() {
		event.Timestamp = time.Now().UTC()
	}
	event.Data = event.Data.Clone()

	e.mu.RLock()
	handlersByID := e.listeners[event.Type]
	handlers := make([]CreditEventHandler, 0, len(handlersByID))
	for _, handler := range handlersByID {
		handlers = append(handlers, handler)
	}
	e.mu.RUnlock()

	for _, handler := range handlers {
		func() {
			defer func() { _ = recover() }()
			handler(ctx, event)
		}()
	}
}

func emitCreditEvent(ctx context.Context, sink CreditEventSink, eventType CreditEventType, userID string, data CreditMetadata) {
	if sink == nil {
		return
	}
	sink.Emit(ctx, CreditEvent{
		Type:      eventType,
		Timestamp: time.Now().UTC(),
		UserID:    userID,
		Data:      data.Clone(),
	})
}
