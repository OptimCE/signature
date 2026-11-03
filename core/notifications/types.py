class NotificationTypes:
    """Notification ``type`` keys this service publishes.

    ``<feature>.<event>`` strings, matching crm-backend's free-form taxonomy.
    The frontend localises the displayed text from the key
    (``NOTIFICATIONS.TYPES.<type>.title``); the backend stores only key + data.

    Every key added here also needs an entry in
    ``crm-frontend/src/app/features/notifications/services/notification-type.registry.ts``,
    a title in all four ``crm-frontend/src/assets/i18n/*.json`` files, and (when
    it carries the email channel) a template in notification-dispatch, or it
    renders to the user as a raw i18n key with no error anywhere.

    - ``signature.requested`` / ``signature.reminder``: to a signer whose turn
      it is (in-app + email, transactional);
    - ``signature.signed``: to the requester, one signer done (in-app);
    - ``signature.declined``: to the requester (in-app + email, transactional);
    - ``signature.completed``: to the requester and every signer (in-app +
      email, informational).

    The requester is the user who asked; a request a job made goes to the
    community's managers.

    This is the ONLY per-service module in ``core/notifications``; the other five
    are byte-identical across producers so Phase 2's extraction is a move rather
    than a rewrite.
    """

    SIGNATURE_REQUESTED = "signature.requested"
    SIGNATURE_REMINDER = "signature.reminder"
    SIGNATURE_SIGNED = "signature.signed"
    SIGNATURE_DECLINED = "signature.declined"
    SIGNATURE_COMPLETED = "signature.completed"
