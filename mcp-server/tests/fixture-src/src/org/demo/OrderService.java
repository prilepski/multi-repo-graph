package org.demo;

import java.util.List;
import org.demo.OrderRepository;

public class OrderService extends AbstractService implements AutoCloseable {
    private final OrderRepository repo;
    protected int counter;

    public void process(String id) {
        validate(id);
        repo.save(id);
        log.info("processed");
    }

    private void validate(String id) {
        if (id == null) { throw new IllegalArgumentException("id"); }
    }

    @Override
    public void close() { cleanup(); }

    void cleanup() { }
}
